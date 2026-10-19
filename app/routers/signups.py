"""线下活动报名。

公开/成员侧：列表、详情（含表单字段）、提交/修改/撤销报名。
管理侧：建活动、改字段和窗口、看名单、导出。

表单字段类型：

- ``text``：单行文本，≤200
- ``textarea``：多行文本，≤2000
- ``number``：数字
- ``select``：单选，``options`` 数组给候选
- ``checkbox``：勾选（布尔）

每字段 ``{key, label, type, required, options?, hint?}``；key 是稳定标识，
已有字段沿用原 key，新字段由客户端生成 ``field_<uuid>``，重试时保持不变。
删掉字段的旧答案保留在原记录里，不能复用已删除的 key。
"""

from __future__ import annotations

import io
import json
import math
import re
from datetime import UTC, datetime

import openpyxl
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import update
from sqlmodel import Session, select

from .. import camps as camps_mod
from ..deps import get_current_user, get_optional_user, get_session, require_admin
from ..models import Platform, Signup, SignupEntry, User, utcnow

router = APIRouter(tags=["signups"])

FIELD_TYPES = {"text", "textarea", "number", "select", "multiselect", "checkbox", "phone", "email"}

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class FieldDef(BaseModel):
    key: str | None = Field(default=None, max_length=64)
    label: str = Field(min_length=1, max_length=60)
    type: str = Field(default="text")
    required: bool = False
    options: list[str] = Field(default_factory=list)
    hint: str = Field(default="", max_length=200)


def _parse_fields(raw: list[FieldDef], previous: list[dict] | None = None) -> list[dict]:
    """保留字段身份；编辑时新增字段使用新 key，绝不复用被删字段的编号。"""
    if len(raw) > 20:
        raise HTTPException(status_code=422, detail="最多 20 个字段")
    known = {f["key"] for f in previous or []}
    used: set[str] = set()
    out = []
    for i, f in enumerate(raw, 1):
        key = f.key
        if key in used or (
            key is not None and key not in known and not re.fullmatch(r"field_[0-9a-f]{32}", key)
        ):
            raise HTTPException(status_code=422, detail="字段标识无效或重复，请刷新后重试")
        if key is None:
            if previous is not None:
                raise HTTPException(status_code=422, detail="字段缺少稳定标识，请刷新页面后重试")
            key = f"field_{i}"
        used.add(key)
        label = f.label.strip()
        if not label:
            raise HTTPException(status_code=422, detail=f"第 {i} 个字段标题为空")
        if f.type not in FIELD_TYPES:
            raise HTTPException(status_code=422, detail=f"「{label}」类型不支持：{f.type}")
        if f.type in {"select", "multiselect"} and not [o for o in f.options if o.strip()]:
            raise HTTPException(status_code=422, detail=f"「{label}」是选择但候选为空")
        out.append(
            {
                "key": key,
                "label": label,
                "type": f.type,
                "required": bool(f.required),
                "options": [o.strip() for o in f.options if o.strip()][:50],
                "hint": f.hint.strip(),
            }
        )
    return out


def _fields(s: Signup) -> list[dict]:
    try:
        return json.loads(s.fields_json or "[]")
    except json.JSONDecodeError:
        return []


def _required_platforms(s: Signup) -> list[str]:
    try:
        return [p for p in json.loads(s.required_platforms_json or "[]") if p in Platform.ALL]
    except json.JSONDecodeError:
        return []


def _required_years(s: Signup) -> list[int]:
    try:
        return [y for y in json.loads(s.required_years_json or "[]") if isinstance(y, int)]
    except json.JSONDecodeError:
        return []


def _validate_answers(fields: list[dict], answers: dict) -> dict:
    """按字段定义校验答案。返回净化后的 dict。"""
    if not isinstance(answers, dict):
        raise HTTPException(status_code=422, detail="答案格式不正确")
    out: dict = {}
    for f in fields:
        raw = answers.get(f["key"])
        if f["type"] == "checkbox":
            if raw is not None and not isinstance(raw, bool):
                raise HTTPException(status_code=422, detail=f"「{f['label']}」要是勾选值")
            if f["required"] and raw is not True:
                raise HTTPException(status_code=422, detail=f"请勾选「{f['label']}」")
            out[f["key"]] = raw is True
        elif f["type"] == "number":
            if raw is None or str(raw).strip() == "":
                if f["required"]:
                    raise HTTPException(status_code=422, detail=f"「{f['label']}」必填")
                out[f["key"]] = ""
            else:
                try:
                    if not math.isfinite(float(str(raw).strip())):
                        raise ValueError
                except ValueError:
                    raise HTTPException(
                        status_code=422, detail=f"「{f['label']}」要是数字"
                    ) from None
                out[f["key"]] = str(raw).strip()[:50]
        elif f["type"] == "select":
            v = str(raw or "").strip()
            if v and v not in f["options"]:
                raise HTTPException(status_code=422, detail=f"「{f['label']}」不在候选里")
            if f["required"] and not v:
                raise HTTPException(status_code=422, detail=f"「{f['label']}」必选")
            out[f["key"]] = v
        elif f["type"] == "multiselect":
            # 复选组：答案是数组，每个都得在候选里。
            pool = raw if isinstance(raw, list) else [raw]
            vals = [str(v).strip() for v in pool if str(v or "").strip()]
            bad = [v for v in vals if v not in f["options"]]
            if bad:
                raise HTTPException(status_code=422, detail=f"「{f['label']}」有不在候选里的值")
            if f["required"] and not vals:
                raise HTTPException(status_code=422, detail=f"「{f['label']}」至少选一项")
            out[f["key"]] = list(dict.fromkeys(vals))
        elif f["type"] == "phone":
            v = str(raw or "").strip()
            if not v:
                if f["required"]:
                    raise HTTPException(status_code=422, detail=f"「{f['label']}」必填")
            elif not PHONE_RE.fullmatch(v):
                raise HTTPException(status_code=422, detail=f"「{f['label']}」要填 11 位手机号")
            out[f["key"]] = v
        elif f["type"] == "email":
            v = str(raw or "").strip()
            if not v:
                if f["required"]:
                    raise HTTPException(status_code=422, detail=f"「{f['label']}」必填")
            elif len(v) > 200 or not EMAIL_RE.fullmatch(v):
                raise HTTPException(status_code=422, detail=f"「{f['label']}」格式不对")
            out[f["key"]] = v
        else:
            v = str(raw or "").strip()
            limit = 2000 if f["type"] == "textarea" else 200
            if len(v) > limit:
                raise HTTPException(status_code=422, detail=f"「{f['label']}」超长了")
            if f["required"] and not v:
                raise HTTPException(status_code=422, detail=f"「{f['label']}」必填")
            out[f["key"]] = v
    return out


def _status(s: Signup, now: datetime) -> str:
    """open / upcoming / closed。close_at 空 = 一直开着。"""
    if now < s.open_at:
        return "upcoming"
    if s.close_at is not None and now >= s.close_at:
        return "closed"
    return "open"


class SignupSummary(BaseModel):
    id: int
    name: str
    description: str
    open_at: datetime
    close_at: datetime | None
    status: str
    capacity: int | None
    entry_count: int
    my_entry: bool = False
    fields: list[dict] = []
    #: 前置条件：这些平台的账号都要已验证。空 = 不设限制。
    required_platforms: list[str] = []
    #: 前置条件：仅限这些入学年级。空 = 不限。
    required_years: list[int] = []


class EntryOut(BaseModel):
    id: int
    user_id: int
    display_name: str
    student_id: str
    class_label: str | None
    answers: dict
    created_at: datetime
    updated_at: datetime


def _summary(s: Signup, db: Session, now: datetime, user: User | None = None) -> SignupSummary:
    count = len(db.exec(select(SignupEntry).where(SignupEntry.signup_id == s.id)).all())
    my = False
    if user is not None:
        my = (
            db.exec(
                select(SignupEntry).where(
                    SignupEntry.signup_id == s.id, SignupEntry.user_id == user.id
                )
            ).first()
            is not None
        )
    return SignupSummary(
        id=s.id or 0,
        name=s.name,
        description=s.description,
        open_at=s.open_at,
        close_at=s.close_at,
        status=_status(s, now),
        capacity=s.capacity,
        entry_count=count,
        my_entry=my,
        fields=_fields(s),
        required_platforms=_required_platforms(s),
        required_years=_required_years(s),
    )


# --------------------------------------------------------------------------- #
# 公开 / 成员
# --------------------------------------------------------------------------- #


@router.get("/signups", response_model=list[SignupSummary])
def list_signups(
    db: Session = Depends(get_session),
    user: User | None = Depends(get_optional_user),
) -> list[SignupSummary]:
    now = datetime.now(UTC)
    rows = db.exec(select(Signup).order_by(Signup.open_at.desc())).all()  # type: ignore[attr-defined]
    return [_summary(s, db, now, user) for s in rows]


@router.get("/signups/{signup_id}", response_model=SignupSummary)
def get_signup(
    signup_id: int,
    db: Session = Depends(get_session),
    user: User | None = Depends(get_optional_user),
) -> SignupSummary:
    s = db.get(Signup, signup_id)
    if s is None:
        raise HTTPException(status_code=404, detail="没有这个报名")
    return _summary(s, db, datetime.now(UTC), user)


@router.get("/signups/{signup_id}/entries/me")
def my_entry(
    signup_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    entry = db.exec(
        select(SignupEntry).where(
            SignupEntry.signup_id == signup_id, SignupEntry.user_id == user.id
        )
    ).first()
    if entry is None:
        return {"answers": {}}
    return {"answers": json.loads(entry.answers_json or "{}"), "created_at": entry.created_at}


class SubmitEntry(BaseModel):
    answers: dict


def _lock_signup(db: Session, signup_id: int) -> Signup | None:
    # 报名名额检查、更新字段、撤销都在同一个 SQLite 写事务内完成。
    db.exec(update(Signup).where(Signup.id == signup_id).values(name=Signup.name))
    return db.get(Signup, signup_id, populate_existing=True)


@router.post("/signups/{signup_id}/entries", status_code=200)
def submit_entry(
    signup_id: int,
    payload: SubmitEntry,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    s = _lock_signup(db, signup_id)
    if s is None:
        raise HTTPException(status_code=404, detail="没有这个报名")
    now = datetime.now(UTC)
    status = _status(s, now)
    if status == "upcoming":
        raise HTTPException(status_code=409, detail="报名还没开始")
    if status == "closed":
        raise HTTPException(status_code=409, detail="报名已截止")

    # 前置条件：所选平台的账号都要绑定并验证（AND，和集训的「至少
    # 一个」不一样——集训是筛谁能计分，这里是入场资格要求）。
    needed = set(_required_platforms(s))
    if needed:
        have = camps_mod.verified_platforms(db, user.id or 0)
        missing = needed - have
        if missing:
            labels = "、".join(Platform.LABELS.get(p, p) for p in sorted(missing))
            raise HTTPException(status_code=403, detail=f"需要先绑定并验证这些平台的账号：{labels}")

    # 年级限制：仅限指定级。管理端也受这个限——名单的干净比绕过更重要。
    years = _required_years(s)
    if years and user.enroll_year not in years:
        label = "、".join(f"{y} 级" for y in sorted(years))
        raise HTTPException(status_code=403, detail=f"本次报名仅限 {label}同学")

    fields = _fields(s)
    answers = _validate_answers(fields, payload.answers)

    entry = db.exec(
        select(SignupEntry).where(
            SignupEntry.signup_id == signup_id, SignupEntry.user_id == user.id
        )
    ).first()
    if entry is None:
        # 名额检查：只在新建时卡，改答案不占新名额。
        if s.capacity is not None:
            count = len(db.exec(select(SignupEntry).where(SignupEntry.signup_id == s.id)).all())
            if count >= s.capacity:
                raise HTTPException(status_code=409, detail="名额已满")
        entry = SignupEntry(
            signup_id=signup_id,
            user_id=user.id or 0,
            answers_json=json.dumps(answers, ensure_ascii=False),
        )
        db.add(entry)
        message = "已报名"
    else:
        previous = json.loads(entry.answers_json or "{}")
        merged = {**previous, **answers}
        if merged != previous:
            entry.answers_json = json.dumps(merged, ensure_ascii=False)
            entry.updated_at = utcnow()
            db.add(entry)
        message = "已更新报名信息"
    db.commit()
    return {"ok": True, "message": message}


@router.delete("/signups/{signup_id}/entries/me", status_code=204)
def withdraw_entry(
    signup_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> None:
    s = _lock_signup(db, signup_id)
    if s is None:
        raise HTTPException(status_code=404, detail="没有这个报名")
    entry = db.exec(
        select(SignupEntry).where(
            SignupEntry.signup_id == signup_id, SignupEntry.user_id == user.id
        )
    ).first()
    if entry is None:
        db.commit()
        return
    if _status(s, datetime.now(UTC)) != "open":
        raise HTTPException(status_code=409, detail="只能在报名开放期间撤销")
    db.delete(entry)
    db.commit()


# --------------------------------------------------------------------------- #
# 管理
# --------------------------------------------------------------------------- #


class SignupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    fields: list[FieldDef] = Field(default_factory=list)
    #: 前置条件：这些平台的账号都要绑定并验证。
    required_platforms: list[str] = Field(default_factory=list)
    #: 前置条件：仅限这些入学年级。
    required_years: list[int] = Field(default_factory=list)
    open_at: AwareDatetime
    close_at: AwareDatetime | None = None
    capacity: int | None = Field(default=None, ge=1)


class SignupUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    fields: list[FieldDef] | None = None
    required_platforms: list[str] | None = None
    required_years: list[int] | None = None
    open_at: AwareDatetime | None = None
    close_at: AwareDatetime | None = None
    capacity: int | None = Field(default=None, ge=1)


@router.get("/admin/signups", response_model=list[SignupSummary])
def admin_list(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> list[SignupSummary]:
    now = datetime.now(UTC)
    rows = db.exec(select(Signup).order_by(Signup.open_at.desc())).all()  # type: ignore[attr-defined]
    return [_summary(s, db, now) for s in rows]


@router.post("/admin/signups", response_model=SignupSummary, status_code=201)
def admin_create(
    payload: SignupCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> SignupSummary:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="名称不能为空")
    if payload.close_at is not None and payload.close_at <= payload.open_at:
        raise HTTPException(status_code=422, detail="截止时间要晚于开放时间")
    platforms = [p for p in payload.required_platforms if p in Platform.ALL]
    years = [y for y in payload.required_years if isinstance(y, int)]
    s = Signup(
        name=name,
        description=payload.description.strip(),
        fields_json=json.dumps(_parse_fields(payload.fields), ensure_ascii=False),
        required_platforms_json=json.dumps(platforms, ensure_ascii=False),
        required_years_json=json.dumps(years, ensure_ascii=False),
        open_at=payload.open_at,
        close_at=payload.close_at,
        capacity=payload.capacity,
        created_by=admin.id or 0,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _summary(s, db, datetime.now(UTC))


@router.put("/admin/signups/{signup_id}", response_model=SignupSummary)
def admin_update(
    signup_id: int,
    payload: SignupUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> SignupSummary:
    s = _lock_signup(db, signup_id)
    if s is None:
        raise HTTPException(status_code=404, detail="没有这个报名")
    fields = payload.model_fields_set
    if "name" in fields:
        name = (payload.name or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="名称不能为空")
        s.name = name
    if "description" in fields:
        s.description = (payload.description or "").strip()
    if "fields" in fields and payload.fields is not None:
        previous = _fields(s)
        current_keys = {f["key"] for f in previous}
        new_keys = {f.key for f in payload.fields} - current_keys
        if new_keys:
            for entry in db.exec(
                select(SignupEntry).where(SignupEntry.signup_id == signup_id)
            ).all():
                if new_keys & set(json.loads(entry.answers_json)):
                    raise HTTPException(status_code=422, detail="不能复用已删除字段的标识")
        s.fields_json = json.dumps(_parse_fields(payload.fields, previous), ensure_ascii=False)
    if "required_platforms" in fields and payload.required_platforms is not None:
        platforms = [p for p in payload.required_platforms if p in Platform.ALL]
        s.required_platforms_json = json.dumps(platforms, ensure_ascii=False)
    if "required_years" in fields and payload.required_years is not None:
        years = [y for y in payload.required_years if isinstance(y, int)]
        s.required_years_json = json.dumps(years, ensure_ascii=False)
    if "open_at" in fields and payload.open_at is not None:
        s.open_at = payload.open_at
    if "close_at" in fields:
        s.close_at = payload.close_at
    if "capacity" in fields:
        s.capacity = payload.capacity
    if s.close_at is not None and s.close_at <= s.open_at:
        raise HTTPException(status_code=422, detail="截止时间要晚于开放时间")
    db.add(s)
    db.commit()
    db.refresh(s)
    return _summary(s, db, datetime.now(UTC))


@router.delete("/admin/signups/{signup_id}", status_code=204)
def admin_delete(
    signup_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> None:
    s = db.get(Signup, signup_id)
    if s is None:
        raise HTTPException(status_code=404, detail="没有这个报名")
    for e in db.exec(select(SignupEntry).where(SignupEntry.signup_id == signup_id)).all():
        db.delete(e)
    # Signup 和 SignupEntry 之间没有 ORM relationship，session 不知道子行
    # 该先删，直接把父行删了会撞外键。先 flush 把子行落下去。
    db.flush()
    db.delete(s)
    db.commit()


@router.get("/admin/signups/{signup_id}/entries", response_model=list[EntryOut])
def admin_entries(
    signup_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> list[EntryOut]:
    rows = db.exec(
        select(SignupEntry)
        .where(SignupEntry.signup_id == signup_id)
        .order_by(SignupEntry.created_at)  # type: ignore[attr-defined]
    ).all()
    users = {
        u.id: u
        for u in db.exec(
            select(User).where(User.id.in_({r.user_id for r in rows}))  # type: ignore[attr-defined]
        ).all()
    }
    return [
        EntryOut(
            id=e.id or 0,
            user_id=e.user_id,
            display_name=users.get(e.user_id).display_name if users.get(e.user_id) else "",
            student_id=users.get(e.user_id).student_id if users.get(e.user_id) else "",
            class_label=users.get(e.user_id).class_label if users.get(e.user_id) else None,
            answers=json.loads(e.answers_json or "{}"),
            created_at=e.created_at,
            updated_at=e.updated_at,
        )
        for e in rows
    ]


@router.delete("/admin/signups/{signup_id}/entries/{entry_id}", status_code=204)
def admin_remove_entry(
    signup_id: int,
    entry_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> None:
    entry = db.get(SignupEntry, entry_id)
    if entry is None or entry.signup_id != signup_id:
        raise HTTPException(status_code=404, detail="没有这条报名")
    db.delete(entry)
    db.commit()


@router.get("/admin/signups/{signup_id}/export")
def admin_export(
    signup_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> Response:
    s = db.get(Signup, signup_id)
    if s is None:
        raise HTTPException(status_code=404, detail="没有这个报名")
    fields = _fields(s)
    entries = admin_entries(signup_id, admin, db)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = s.name[:28]
    headers = ["姓名", "学号", "班级"] + [f["label"] for f in fields] + ["报名时间", "更新时间"]
    ws.append(headers)
    for e in entries:
        row = [e.display_name, e.student_id, e.class_label or ""]
        for f in fields:
            v = e.answers.get(f["key"], "")
            if f["type"] == "checkbox":
                v = "是" if v else ""
            elif isinstance(v, list):
                v = "、".join(str(x) for x in v)
            row.append(v)
        row += [e.created_at.strftime("%Y-%m-%d %H:%M"), e.updated_at.strftime("%Y-%m-%d %H:%M")]
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"signup-{s.id}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
