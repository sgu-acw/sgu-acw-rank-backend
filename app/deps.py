"""Shared FastAPI dependencies: current user, role checks, origin guard."""

from __future__ import annotations

import hmac
from datetime import timedelta

from fastapi import Depends, Header, HTTPException, Request, status
from sqlmodel import Session, select

from .config import get_settings
from .db import get_session
from .models import Role, User, UserSession, UserStatus, utcnow
from .security import hash_session_token

#: 仅当距离上次记录超过该间隔时才回写 last_seen_at，避免只读请求频繁抢占 SQLite 写锁。
SESSION_TOUCH_INTERVAL = timedelta(minutes=5)


def require_origin_secret(
    x_origin_secret: str | None = Header(default=None, alias="X-Origin-Secret"),
) -> None:
    """Reject requests that did not come through the Pages Function.

    The backend listens on the VPS directly, so without this anyone who learns
    the hostname can bypass Cloudflare and talk to it. An empty configured
    secret disables the check, which is what local development wants.
    """
    expected = get_settings().origin_secret
    if not expected:
        return
    if not x_origin_secret or not hmac.compare_digest(x_origin_secret, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="拒绝访问")


def get_current_session(
    request: Request,
    db: Session = Depends(get_session),
) -> UserSession | None:
    """Resolve the session cookie to a row, or None when absent/invalid."""
    settings = get_settings()
    token = request.cookies.get(settings.cookie_name)
    if not token:
        return None

    row = db.exec(
        select(UserSession).where(UserSession.token_hash == hash_session_token(token))
    ).first()
    if row is None or row.is_expired:
        return None

    now = utcnow()
    if row.last_seen_at is None or (now - row.last_seen_at) >= SESSION_TOUCH_INTERVAL:
        row.last_seen_at = now
        db.add(row)
        db.commit()
    return row


def get_current_user(
    session_row: UserSession | None = Depends(get_current_session),
    db: Session = Depends(get_session),
) -> User:
    """The authenticated user, or 401."""
    if session_row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")

    user = db.get(User, session_row.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    if user.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已停用")
    return user


def get_optional_user(
    session_row: UserSession | None = Depends(get_current_session),
    db: Session = Depends(get_session),
) -> User | None:
    """The authenticated user if there is one, else None. Never raises."""
    if session_row is None:
        return None
    user = db.get(User, session_row.user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        return None
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Any administrative role (root or admin)."""
    if user.role not in Role.ADMINISTRATIVE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def require_root(user: User = Depends(get_current_user)) -> User:
    """Root only, for operations that must not spread sideways."""
    if user.role != Role.ROOT:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要 root 权限")
    return user


def require_editorial_secret(
    x_editorial_secret: str | None = Header(default=None, alias="X-Editorial-Secret"),
) -> None:
    """Gate for ``/internal/*``: the editorial generator's shared secret.

    Separate from ``require_origin_secret`` so the two credentials never stand
    in for each other. The generator runs on the VPS next to the API and must
    not be able to call the public API as though it were the CDN; conversely a
    request that merely came through Cloudflare must not be able to write
    editorials.

    An unconfigured secret disables the routes rather than opening them: this
    endpoint writes content that members read, so failing closed is the only
    safe default.
    """
    expected = get_settings().editorial_secret
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="题解生成接口未启用",
        )
    if not x_editorial_secret or not hmac.compare_digest(x_editorial_secret, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="拒绝访问")
