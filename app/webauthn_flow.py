"""通行密钥（WebAuthn）注册的注册/登录流程。

挑战存库（``webauthn_challenges``）而不是签进 cookie：一次性、可过期，
不需要服务器多记一份状态。挑战生成只发生在这里，调用方负责 commit。
"""

from __future__ import annotations

import json
import secrets
from datetime import timedelta

import webauthn
from fastapi import HTTPException
from sqlalchemy import delete
from sqlmodel import Session, select
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .config import get_settings
from .models import PasskeyCredential, User, WebAuthnChallenge, utcnow

CHALLENGE_TTL = timedelta(minutes=5)


def _purge_expired(db: Session) -> None:
    for row in db.exec(
        select(WebAuthnChallenge).where(WebAuthnChallenge.expires_at < utcnow())
    ).all():
        db.delete(row)


def _store_challenge(db: Session, *, kind: str, user_id: int | None) -> str:
    challenge = secrets.token_urlsafe(32)
    db.add(
        WebAuthnChallenge(
            challenge=challenge,
            kind=kind,
            user_id=user_id,
            expires_at=utcnow() + CHALLENGE_TTL,
        )
    )
    return challenge


def _load_challenge(
    db: Session,
    raw: str,
    kind: str,
    *,
    user_id: int | None = None,
) -> None:
    # 原子消费，避免两个请求先读到同一挑战后分别创建会话。
    consumed = db.exec(
        delete(WebAuthnChallenge).where(
            WebAuthnChallenge.challenge == raw,
            WebAuthnChallenge.kind == kind,
            WebAuthnChallenge.user_id == user_id,
            WebAuthnChallenge.expires_at > utcnow(),
        )
    )
    if consumed.rowcount != 1:
        raise HTTPException(status_code=422, detail="挑战已过期、已使用或不属于当前账号，请重试")


# --------------------------------------------------------------------------- #
# 注册（已登录的成员给当前账号绑一台设备）
# --------------------------------------------------------------------------- #


def begin_register(db: Session, user: User) -> dict:
    s = get_settings()
    _purge_expired(db)
    challenge = _store_challenge(db, kind="register", user_id=user.id)

    existing = db.exec(select(PasskeyCredential).where(PasskeyCredential.user_id == user.id)).all()
    exclude = [
        PublicKeyCredentialDescriptor(
            id=base64url_to_bytes(c.credential_id),
            transports=[
                AuthenticatorTransport(t)
                for t in c.transports.split(",")
                if t in {v.value for v in AuthenticatorTransport}
            ]
            or None,
        )
        for c in existing
    ]

    options = webauthn.generate_registration_options(
        rp_id=s.rp_id,
        rp_name=s.rp_name,
        user_id=str(user.id).encode(),
        user_name=user.student_id,
        user_display_name=user.display_name,
        challenge=base64url_to_bytes(challenge),
        authenticator_selection=AuthenticatorSelectionCriteria(
            # 登录时不先问学号——要求 authenticator 存可发现凭据，
            # 让登录端能按 RP 直接列出这个人的通行密钥。
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=exclude or None,
        timeout=int(CHALLENGE_TTL.total_seconds() * 1000),
    )
    return json.loads(webauthn.options_to_json(options))


def finish_register(
    db: Session, user: User, challenge: str, credential: dict, name: str
) -> PasskeyCredential:
    s = get_settings()
    _load_challenge(db, challenge, "register", user_id=user.id)

    try:
        verification = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=s.rp_id,
            expected_origin=s.rp_origin,
            require_user_verification=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"验证失败：{exc}") from exc

    cred_id = bytes_to_base64url(verification.credential_id)
    if db.exec(select(PasskeyCredential).where(PasskeyCredential.credential_id == cred_id)).first():
        raise HTTPException(status_code=409, detail="这个通行密钥已经绑过了")

    # transports 不在验证结果里（webauthn 3.x），从提交的凭据里拿。
    transports = credential.get("response", {}).get("transports") or []
    row = PasskeyCredential(
        user_id=user.id or 0,
        credential_id=cred_id,
        public_key=bytes_to_base64url(verification.credential_public_key),
        sign_count=verification.sign_count,
        transports=",".join(str(t) for t in transports),
        name=name.strip()[:80],
    )
    db.add(row)
    return row


# --------------------------------------------------------------------------- #
# 登录（匿名——密钥自己说清楚是谁）
# --------------------------------------------------------------------------- #


def begin_login(db: Session) -> dict:
    s = get_settings()
    challenge = _store_challenge(db, kind="login", user_id=None)
    _purge_expired(db)
    options = webauthn.generate_authentication_options(
        rp_id=s.rp_id,
        challenge=base64url_to_bytes(challenge),
        user_verification=UserVerificationRequirement.REQUIRED,
        timeout=int(CHALLENGE_TTL.total_seconds() * 1000),
    )
    return json.loads(webauthn.options_to_json(options))


def finish_login(db: Session, challenge: str, credential: dict) -> tuple[User, PasskeyCredential]:
    s = get_settings()
    _load_challenge(db, challenge, "login")

    cred_id = credential.get("id") or ""
    row = db.exec(
        select(PasskeyCredential).where(PasskeyCredential.credential_id == cred_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=401, detail="这个通行密钥不在册")

    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="密钥对应的账号已不存在")

    try:
        verification = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=s.rp_id,
            expected_origin=s.rp_origin,
            credential_public_key=base64url_to_bytes(row.public_key),
            credential_current_sign_count=row.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"验证失败：{exc}") from exc

    row.sign_count = verification.new_sign_count
    row.last_used_at = utcnow()
    db.add(row)
    return user, row
