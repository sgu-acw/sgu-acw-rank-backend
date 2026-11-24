"""通行密钥（WebAuthn passkey）接口。

``/me/passkeys/*`` 是已登录成员管理自己密钥的：注册开始/结束、列、删。
``/auth/passkey/*`` 是匿名的通行密钥登录：挑战开始 → 验证签名 → 发会话。

注册时用 ``resident_key=REQUIRED`` 生成可发现凭据——登录端不用先问学号，
浏览器自己知道这台设备上有谁的密钥。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from .. import webauthn_flow
from ..deps import get_current_user, get_session, require_origin_secret
from ..models import PasskeyCredential, User, UserStatus
from .auth import _issue_session, _set_session_cookie  # noqa: PLC2701

router = APIRouter(tags=["passkeys"], dependencies=[Depends(require_origin_secret)])


class PasskeyOut(BaseModel):
    id: int
    name: str
    transports: str
    created_at: datetime
    last_used_at: datetime | None


def _out(c: PasskeyCredential) -> PasskeyOut:
    return PasskeyOut(
        id=c.id or 0,
        name=c.name,
        transports=c.transports,
        created_at=c.created_at,
        last_used_at=c.last_used_at,
    )


# --------------------------------------------------------------------------- #
# 已登录：管理自己的通行密钥
# --------------------------------------------------------------------------- #


@router.get("/me/passkeys", response_model=list[PasskeyOut])
def list_mine(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[PasskeyOut]:
    rows = db.exec(
        select(PasskeyCredential)
        .where(PasskeyCredential.user_id == user.id)
        .order_by(PasskeyCredential.created_at)  # type: ignore[attr-defined]
    ).all()
    return [_out(c) for c in rows]


@router.post("/me/passkeys/register/begin")
def register_begin(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    options = webauthn_flow.begin_register(db, user)
    db.commit()
    return {"options": options}


class RegisterComplete(BaseModel):
    challenge: str
    credential: dict
    name: str = Field(default="", max_length=80)


@router.post("/me/passkeys/register/complete", response_model=PasskeyOut, status_code=201)
def register_complete(
    payload: RegisterComplete,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> PasskeyOut:
    row = webauthn_flow.finish_register(
        db, user, payload.challenge, payload.credential, payload.name
    )
    db.commit()
    db.refresh(row)
    return _out(row)


@router.delete("/me/passkeys/{credential_row_id}", status_code=204)
def remove_mine(
    credential_row_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> None:
    row = db.get(PasskeyCredential, credential_row_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="没有这个密钥")
    db.delete(row)
    db.commit()


# --------------------------------------------------------------------------- #
# 匿名：通行密钥登录
# --------------------------------------------------------------------------- #


@router.post("/auth/passkey/login/begin")
def login_begin(db: Session = Depends(get_session)) -> dict:
    options = webauthn_flow.begin_login(db)
    db.commit()
    return {"options": options}


class LoginComplete(BaseModel):
    challenge: str
    credential: dict


@router.post("/auth/passkey/login/complete")
def login_complete(
    payload: LoginComplete,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
) -> dict:
    user, _cred = webauthn_flow.finish_login(db, payload.challenge, payload.credential)
    if user.status == UserStatus.PENDING:
        raise HTTPException(status_code=403, detail="账号正在等待管理员审核，审核通过后即可登录")
    if user.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="账号已停用")

    user.last_login_at = webauthn_flow.utcnow()
    db.add(user)
    token = _issue_session(db, user, request)
    db.commit()
    db.refresh(user)
    _set_session_cookie(response, token)
    return {"user": _to_self(user), "totp_required": False}


def _to_self(user: User) -> dict:
    """登录回包要的最小集——比 UserSelf 里公开字段少传不掉，懒省事直接用
    UserSelf 的序列化。"""
    from ..schemas import UserSelf

    return UserSelf.model_validate(user).model_dump()
