"""全体管理员站内信；列表共享，已读状态互不影响。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, func, select

from ..deps import get_session, require_admin
from ..models import AdminNotification, NotificationRead, User

router = APIRouter(prefix="/admin/notifications", tags=["notifications"])


def unread_condition(user_id: int):
    read_ids = (
        select(NotificationRead.notification_id)
        .where(
            NotificationRead.user_id == user_id,
            NotificationRead.revision >= AdminNotification.revision,
        )
        .correlate(AdminNotification)
    )
    return AdminNotification.id.not_in(read_ids)


@router.get("")
def list_notifications(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    unread_only: bool = False,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> dict:
    unread = unread_condition(user.id)
    unread_count = db.exec(select(func.count()).select_from(AdminNotification).where(unread)).one()
    query = select(AdminNotification)
    count = select(func.count()).select_from(AdminNotification)
    if unread_only:
        query = query.where(unread)
        count = count.where(unread)
    total = db.exec(count).one()
    rows = db.exec(
        query.order_by(AdminNotification.updated_at.desc(), AdminNotification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    reads = {
        r.notification_id: r.revision
        for r in db.exec(
            select(NotificationRead).where(
                NotificationRead.user_id == user.id,
                NotificationRead.notification_id.in_([r.id for r in rows]),
            )
        ).all()
    }
    return {
        "items": [
            {**r.model_dump(exclude={"fingerprint"}), "read": reads.get(r.id, 0) >= r.revision}
            for r in rows
        ],
        "total": total,
        "unread_count": unread_count,
        "page": page,
        "page_size": page_size,
    }


@router.get("/unread-count")
def unread_count(user: User = Depends(require_admin), db: Session = Depends(get_session)) -> dict:
    count = db.exec(
        select(func.count()).select_from(AdminNotification).where(unread_condition(user.id))
    ).one()
    return {"count": count}


def mark_read(db: Session, user_id: int, row: AdminNotification) -> None:
    statement = insert(NotificationRead).values(
        notification_id=row.id,
        user_id=user_id,
        revision=row.revision,
    )
    db.exec(
        statement.on_conflict_do_update(
            index_elements=["notification_id", "user_id"],
            set_={"revision": func.max(NotificationRead.revision, row.revision)},
        )
    )


@router.post("/read-all")
def read_all(user: User = Depends(require_admin), db: Session = Depends(get_session)) -> dict:
    for row in db.exec(select(AdminNotification).where(unread_condition(user.id))).all():
        mark_read(db, user.id, row)
    db.commit()
    return {"ok": True}


@router.post("/{notification_id}/read")
def read_one(
    notification_id: int, user: User = Depends(require_admin), db: Session = Depends(get_session)
) -> dict:
    row = db.get(AdminNotification, notification_id)
    if row is None:
        raise HTTPException(status_code=404, detail="站内信不存在")
    mark_read(db, user.id, row)
    db.commit()
    return {"ok": True}
