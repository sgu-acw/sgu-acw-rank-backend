"""Web Push 订阅与管理员群发。

订阅管理面向所有登录成员：一个成员可以有多个浏览器订阅（手机、电脑）。
群发面向管理员：按群体挑人，把通知发到他们所有开了订阅的设备上。
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, select

from .. import push as push_mod
from ..deps import get_current_user, get_session, require_admin
from ..models import (
    CampRegistration,
    PushDispatch,
    PushSubscription,
    Role,
    User,
    UserStatus,
)

router = APIRouter(tags=["push"])


class SubscribeIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=1000)
    p256dh: str = Field(min_length=1, max_length=255)
    auth: str = Field(min_length=1, max_length=255)


class UnsubscribeIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=1000)


class AnnounceIn(BaseModel):
    request_id: str = Field(min_length=16, max_length=64)
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(default="", max_length=300)
    url: str = Field(default="/", max_length=200)
    #: all | admins | camp:<id>
    audience: str = Field(default="all", max_length=6000)


@router.get("/push/vapid-key")
def vapid_key() -> dict:
    """给浏览器拿公钥，订阅时要用。公钥本来就是公开的。"""
    return {"key": push_mod.public_key(), "configured": push_mod.configured()}


@router.post("/push/subscribe", status_code=201)
def subscribe(
    payload: SubscribeIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    # endpoint 是自然唯一键；并发重订阅执行原子 upsert。
    statement = insert(PushSubscription).values(
        user_id=user.id,
        endpoint=payload.endpoint,
        p256dh=payload.p256dh,
        auth=payload.auth,
        user_agent=(request.headers.get("user-agent") or "")[:300],
    )
    db.exec(
        statement.on_conflict_do_update(
            index_elements=["endpoint"],
            set_={
                key: getattr(statement.excluded, key)
                for key in ("user_id", "p256dh", "auth", "user_agent")
            },
        )
    )
    db.commit()
    return {"ok": True}


@router.post("/push/unsubscribe", status_code=204)
def unsubscribe(
    payload: UnsubscribeIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> None:
    for row in db.exec(
        select(PushSubscription).where(
            PushSubscription.endpoint == payload.endpoint,
            PushSubscription.user_id == user.id,
        )
    ).all():
        db.delete(row)
    db.commit()


def _audience_user_ids(db: Session, audience: str) -> list[int]:
    """把群体字符串解析成 user_id 列表。"""
    if audience == "all":
        return [
            u.id
            for u in db.exec(select(User).where(User.status == UserStatus.ACTIVE)).all()
            if u.id
        ]
    if audience == "admins":
        return [
            u.id
            for u in db.exec(select(User).where(User.role.in_(Role.ADMINISTRATIVE))).all()  # type: ignore[attr-defined]
            if u.id
        ]
    if audience.startswith("camp:"):
        try:
            camp_id = int(audience[5:])
        except ValueError:
            raise HTTPException(status_code=422, detail="非法群体") from None
        return list(
            db.exec(
                select(CampRegistration.user_id).where(CampRegistration.camp_id == camp_id)
            ).all()
        )
    if audience.startswith("year:"):
        try:
            year = int(audience[5:])
        except ValueError:
            raise HTTPException(status_code=422, detail="非法群体") from None
        return [
            u.id
            for u in db.exec(
                select(User).where(User.status == UserStatus.ACTIVE, User.enroll_year == year)
            ).all()
            if u.id
        ]
    if audience.startswith("users:"):
        # 指定成员：前端搜索+勾选后把 id 列表传上来。
        try:
            ids = [int(part) for part in audience[6:].split(",") if part.strip()]
        except ValueError:
            raise HTTPException(status_code=422, detail="非法群体") from None
        if not ids or len(ids) > 500:
            raise HTTPException(status_code=422, detail="成员列表为空或过多")
        return [
            u.id
            for u in db.exec(
                select(User).where(User.id.in_(ids), User.status == UserStatus.ACTIVE)  # type: ignore[attr-defined]
            ).all()
            if u.id
        ]
    raise HTTPException(status_code=422, detail="非法群体")


@router.post("/admin/push/announce")
def announce(
    payload: AnnounceIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> dict:
    if not push_mod.configured():
        raise HTTPException(status_code=503, detail="推送未配置（缺 VAPID 密钥）")
    digest = hashlib.sha256(
        json.dumps(
            payload.model_dump(exclude={"request_id"}),
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    claim = db.exec(
        insert(PushDispatch)
        .values(
            request_id=payload.request_id,
            user_id=admin.id,
            payload_hash=digest,
        )
        .on_conflict_do_nothing(index_elements=["request_id"])
    )
    row = db.get(PushDispatch, payload.request_id)
    if claim.rowcount != 1:
        db.commit()
        if row.user_id != admin.id or row.payload_hash != digest:
            raise HTTPException(status_code=409, detail="这次发送的内容已变化，请重新打开发送窗口")
        if row.state == "completed":
            return json.loads(row.result_json)
        raise HTTPException(status_code=409, detail="这次推送仍在处理或结果未确认，请勿重复发送")
    user_ids = _audience_user_ids(db, payload.audience)
    db.commit()  # 先持久化占位；外部请求期间不持有数据库写锁。
    try:
        result = push_mod.send_to_user_ids(
            db, user_ids, {"title": payload.title, "body": payload.body, "url": payload.url}
        )
    except Exception:
        db.rollback()
        row.state = "uncertain"
        db.add(row)
        db.commit()
        raise
    response = {"audience": len(user_ids), **result}
    row.state = "completed"
    row.result_json = json.dumps(response)
    db.add(row)
    db.commit()
    return response
