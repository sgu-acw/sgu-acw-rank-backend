"""工作坊状态的手动覆盖与排期。

心跳服务只会说「设备在不在」，「赛事占用」「考试周闭馆」这种状态它说不出
来。管理员在这里登记一条：可以立即生效，也可以排到将来某个时段（比如
「明天下午两点集训队比赛」）。到点自动生效、过点自动恢复，不用回来手动关。

可以有多条排期并存；同一时刻生效的取最新写的那条。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field
from sqlmodel import Session, select

from ..deps import get_session, require_admin
from ..models import User, WorkshopStatusOverride

router = APIRouter(tags=["workshop"])


def _live_rows(session: Session) -> list[WorkshopStatusOverride]:
    return session.exec(
        select(WorkshopStatusOverride)
        .where(WorkshopStatusOverride.active.is_(True))
        .order_by(WorkshopStatusOverride.id.desc())
    ).all()


def _in_window(row: WorkshopStatusOverride, now: datetime) -> bool:
    """此刻是否在生效窗口内。"""
    if row.start_at is not None and row.start_at > now:
        return False
    return not (row.until is not None and row.until <= now)


def _current(session: Session) -> WorkshopStatusOverride | None:
    """当前生效的覆盖。排期还没到点、已经过点、被关掉的都不算。"""
    now = datetime.now(UTC)
    for row in _live_rows(session):
        if _in_window(row, now):
            return row
    return None


class OverrideOut(BaseModel):
    id: int
    label: str
    note: str
    start_at: datetime | None
    until: datetime | None
    updated_at: datetime


class OverrideSet(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    note: str = Field(default="", max_length=200)
    #: 留空 = 立即生效；填了 = 排到那个时刻。
    start_at: AwareDatetime | None = None
    #: 留空 = 直到手动关闭；填了 = 到点自动恢复。
    until: AwareDatetime | None = None


def _out(row: WorkshopStatusOverride) -> OverrideOut:
    return OverrideOut(
        id=row.id or 0,
        label=row.label,
        note=row.note,
        start_at=row.start_at,
        until=row.until,
        updated_at=row.updated_at,
    )


@router.get("/workshop/override", response_model=OverrideOut | None)
def get_override(session: Session = Depends(get_session)) -> OverrideOut | None:
    row = _current(session)
    return _out(row) if row else None


@router.get("/workshop/schedule", response_model=list[OverrideOut])
def get_schedule(session: Session = Depends(get_session)) -> list[OverrideOut]:
    """当前生效 + 未来 30 天内排期的状态，公开可读。"""
    now = datetime.now(UTC)
    horizon = now + timedelta(days=30)
    rows = [
        r
        for r in _live_rows(session)
        if (r.until is None or r.until > now) and (r.start_at is None or r.start_at < horizon)
    ]
    rows.sort(key=lambda r: (r.start_at is not None, r.start_at or r.updated_at))
    return [_out(r) for r in rows]


@router.get("/admin/workshop/schedule", response_model=list[OverrideOut])
def admin_schedule(
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[OverrideOut]:
    """管理页可查看所有保留的排期，包括已结束和 30 天以后的状态。"""
    return [_out(row) for row in _live_rows(session)]


@router.put("/admin/workshop/override", response_model=OverrideOut, status_code=201)
def set_override(
    payload: OverrideSet,
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> OverrideOut:
    label = payload.label.strip()
    if not label:
        raise HTTPException(status_code=422, detail="状态词不能为空")
    if (
        payload.start_at is not None
        and payload.until is not None
        and payload.until <= payload.start_at
    ):
        raise HTTPException(status_code=422, detail="结束时间要晚于开始时间")
    now = datetime.now(UTC)
    if payload.until is not None and payload.until <= now:
        raise HTTPException(status_code=422, detail="结束时间已经过去")
    row = WorkshopStatusOverride(
        label=label,
        note=payload.note.strip(),
        start_at=payload.start_at,
        until=payload.until,
        updated_by=user.id or 0,
        updated_at=now,
    )
    session.add(row)
    # 新写的这条如果此刻就在生效窗口里，把其他同样生效中的关掉——
    # 同一时刻只能显示一个状态，最新写的赢。
    if _in_window(row, now):
        for old in _live_rows(session):
            if old is not row and _in_window(old, now):
                old.active = False
                session.add(old)
    session.commit()
    session.refresh(row)
    return _out(row)


class OverridePatch(BaseModel):
    label: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=200)
    # 三个状态：不传 = 不动；传 null = 清空；传值 = 更新。
    # Pydantic 的 ``model_fields_set`` 区分「没传」和「传了 null」。
    start_at: AwareDatetime | None = None
    until: AwareDatetime | None = None


@router.patch("/admin/workshop/override/{override_id}", response_model=OverrideOut)
def update_override(
    override_id: int,
    payload: OverridePatch,
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> OverrideOut:
    row = session.get(WorkshopStatusOverride, override_id)
    if row is None or not row.active:
        raise HTTPException(status_code=404, detail="没有这条状态")

    fields = payload.model_fields_set
    if "label" in fields:
        label = (payload.label or "").strip()
        if not label:
            raise HTTPException(status_code=422, detail="状态词不能为空")
        row.label = label
    if "note" in fields:
        row.note = (payload.note or "").strip()
    if "start_at" in fields:
        row.start_at = payload.start_at
    if "until" in fields:
        row.until = payload.until

    now = datetime.now(UTC)
    if row.start_at is not None and row.until is not None and row.until <= row.start_at:
        raise HTTPException(status_code=422, detail="结束时间要晚于开始时间")
    if row.until is not None and row.until <= now:
        raise HTTPException(status_code=422, detail="结束时间已经过去")

    row.updated_at = now
    row.updated_by = user.id or 0
    session.add(row)
    if _in_window(row, now):
        for old in _live_rows(session):
            if old is not row and _in_window(old, now):
                old.active = False
                session.add(old)
    session.commit()
    session.refresh(row)
    return _out(row)


@router.delete("/admin/workshop/override/{override_id}", status_code=204)
def delete_override(
    override_id: int,
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    row = session.get(WorkshopStatusOverride, override_id)
    if row is None or not row.active:
        raise HTTPException(status_code=404, detail="没有这条状态")
    row.active = False
    session.add(row)
    session.commit()


@router.delete("/admin/workshop/override", status_code=204)
def clear_override(
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    """关掉此刻正在生效的那条（排期未到的不动）。"""
    now = datetime.now(UTC)
    for row in _live_rows(session):
        if _in_window(row, now):
            row.active = False
            session.add(row)
    session.commit()
