"""本地生成真实 P-256 WebAuthn 签名，覆盖挑战归属与一次性消费。"""

# ruff: noqa: F811 -- pytest fixtures are imported for discovery
import hashlib
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import cbor2
import pyotp
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from sqlmodel import Session, select
from webauthn.helpers import bytes_to_base64url

from app import webauthn_flow
from app.config import get_settings
from app.db import get_engine
from app.models import User, utcnow
from tests.test_camps import (  # noqa: F401
    MEMBER_PASSWORD,
    MEMBER_STUDENT_ID,
    client,
    db,
    env,
    member_client,
    root_client,
)


def client_data(challenge, kind):
    return json.dumps(
        {
            "type": kind,
            "challenge": challenge,
            "origin": get_settings().rp_origin,
        },
        separators=(",", ":"),
    ).encode()


def registration(challenge, *, verified=True):
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_numbers()
    cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: public.x.to_bytes(32), -3: public.y.to_bytes(32)})
    credential_id = secrets.token_bytes(32)
    rp_hash = hashlib.sha256(get_settings().rp_id.encode()).digest()
    flags = 0x45 if verified else 0x41
    data = (
        rp_hash
        + bytes([flags])
        + bytes(4)
        + bytes(16)
        + len(credential_id).to_bytes(2)
        + credential_id
        + cose
    )
    return private, {
        "id": bytes_to_base64url(credential_id),
        "rawId": bytes_to_base64url(credential_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": bytes_to_base64url(client_data(challenge, "webauthn.create")),
            "attestationObject": bytes_to_base64url(
                cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": data})
            ),
            "transports": ["internal"],
        },
    }


def authentication(challenge, private, credential_id):
    data = client_data(challenge, "webauthn.get")
    auth_data = hashlib.sha256(get_settings().rp_id.encode()).digest() + b"\x05" + (1).to_bytes(4)
    signature = private.sign(auth_data + hashlib.sha256(data).digest(), ec.ECDSA(hashes.SHA256()))
    return {
        "id": credential_id,
        "rawId": credential_id,
        "type": "public-key",
        "response": {
            "clientDataJSON": bytes_to_base64url(data),
            "authenticatorData": bytes_to_base64url(auth_data),
            "signature": bytes_to_base64url(signature),
        },
    }


def test_signed_passkey_flow_ownership_exclusion_and_replay(member_client, root_client):
    challenge = member_client.post("/api/me/passkeys/register/begin").json()["options"]["challenge"]
    private, credential = registration(challenge)
    payload = {"challenge": challenge, "credential": credential, "name": "本地测试认证器"}
    assert root_client.post("/api/me/passkeys/register/complete", json=payload).status_code == 422
    assert member_client.post("/api/me/passkeys/register/complete", json=payload).status_code == 201
    assert member_client.post("/api/me/passkeys/register/complete", json=payload).status_code == 422
    second = member_client.post("/api/me/passkeys/register/begin")
    assert second.status_code == 200
    assert second.json()["options"]["excludeCredentials"][0]["id"] == credential["id"]
    challenge = member_client.post("/api/auth/passkey/login/begin").json()["options"]["challenge"]
    signed = authentication(challenge, private, credential["id"])
    login = {"challenge": challenge, "credential": signed}
    response = member_client.post("/api/auth/passkey/login/complete", json=login)
    assert response.status_code == 200, response.text
    assert response.json()["user"]["student_id"] == MEMBER_STUDENT_ID
    assert member_client.post("/api/auth/passkey/login/complete", json=login).status_code == 422


def test_passkey_requires_user_verification(member_client):
    challenge = member_client.post("/api/me/passkeys/register/begin").json()["options"]["challenge"]
    _, credential = registration(challenge, verified=False)
    assert (
        member_client.post(
            "/api/me/passkeys/register/complete",
            json={
                "challenge": challenge,
                "credential": credential,
            },
        ).status_code
        == 422
    )


def test_concurrent_challenge_consumption_has_one_winner(member_client):
    challenge = member_client.post("/api/auth/passkey/login/begin").json()["options"]["challenge"]
    barrier = Barrier(2)

    def consume(_):
        with Session(get_engine()) as session:
            barrier.wait(timeout=5)
            try:
                webauthn_flow._load_challenge(session, challenge, "login")
                session.commit()
                return True
            except HTTPException:
                session.rollback()
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume, range(2))) == [False, True]


def enable_totp(member_client):
    first = member_client.post("/api/auth/me/totp/begin").json()
    second = member_client.post("/api/auth/me/totp/begin").json()
    assert second["secret"] == first["secret"]
    code = pyotp.TOTP(first["secret"]).now()
    for _ in range(2):
        assert (
            member_client.post("/api/auth/me/totp/enable", json={"code": code}).status_code == 200
        )
    assert member_client.post("/api/auth/me/totp/begin").status_code == 409
    return first["secret"]


def ticket(member_client):
    return member_client.post(
        "/api/auth/login",
        json={
            "student_id": MEMBER_STUDENT_ID,
            "password": MEMBER_PASSWORD,
        },
    ).json()["ticket"]


def test_totp_tickets_are_unique_bounded_and_codes_cannot_login_twice(member_client):
    secret = enable_totp(member_client)
    first, second = ticket(member_client), ticket(member_client)
    assert first != second
    code = pyotp.TOTP(secret).now()
    assert (
        member_client.post("/api/auth/login/totp", json={"ticket": first, "code": code}).status_code
        == 200
    )
    assert (
        member_client.post("/api/auth/login/totp", json={"ticket": first, "code": code}).status_code
        == 401
    )
    assert (
        member_client.post(
            "/api/auth/login/totp", json={"ticket": second, "code": code}
        ).status_code
        == 401
    )
    # 同一票据最多 5 次尝试，即使第 6 次给正确的新码也不能通过。
    third = ticket(member_client)
    for _ in range(5):
        assert (
            member_client.post(
                "/api/auth/login/totp", json={"ticket": third, "code": "invalid"}
            ).status_code
            == 401
        )
    future = pyotp.TOTP(secret).at(utcnow() + timedelta(seconds=30))
    assert (
        member_client.post(
            "/api/auth/login/totp", json={"ticket": third, "code": future}
        ).status_code
        == 401
    )


def test_pending_totp_setup_expires(member_client, db):
    result = member_client.post("/api/auth/me/totp/begin").json()
    user = db.exec(select(User).where(User.student_id == MEMBER_STUDENT_ID)).one()
    user.totp_pending_expires_at = utcnow() - timedelta(seconds=1)
    db.add(user)
    db.commit()
    assert (
        member_client.post(
            "/api/auth/me/totp/enable", json={"code": pyotp.TOTP(result["secret"]).now()}
        ).status_code
        == 400
    )
