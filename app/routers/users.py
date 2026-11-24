"""Member directory.

Read access is limited to logged-in members: the whole point of moving this
behind an account is that the roster, which pairs real names with platform
handles, is not public.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from ..db import get_session
from ..deps import require_campus_member, require_origin_secret
from ..models import Role, User, UserStatus
from ..schemas import UserPublic

router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(require_origin_secret)],
)


@router.get("", response_model=list[UserPublic])
def list_members(
    _current: User = Depends(require_campus_member),
    db: Session = Depends(get_session),
    include_disabled: bool = Query(default=False),
) -> list[User]:
    """List campus members, newest enrolment first.

    Disabled accounts are hidden unless explicitly asked for, and only an
    administrator may ask. External accounts are never included in the campus
    member directory.
    """
    statement = select(User).where(User.role != Role.EXTERNAL)
    if not include_disabled or not _current.is_admin:
        statement = statement.where(User.status == UserStatus.ACTIVE)
    else:
        statement = statement.where(User.status != UserStatus.PENDING)
    statement = statement.order_by(User.enroll_year.desc(), User.student_id)  # type: ignore[attr-defined]
    return list(db.exec(statement).all())


@router.get("/{user_id}", response_model=UserPublic)
def get_member(
    user_id: int,
    _current: User = Depends(require_campus_member),
    db: Session = Depends(get_session),
) -> User:
    user = db.get(User, user_id)
    if user is None or user.is_external or (
        user.status == UserStatus.PENDING and not _current.is_admin
    ):
        raise HTTPException(status_code=404, detail="成员不存在")
    return user
