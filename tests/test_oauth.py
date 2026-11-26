"""工作坊通用 OAuth 2.0 + PKCE Provider 测试。"""

from __future__ import annotations

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import db as db_module
from app.config import get_settings
from app.main import create_app
from app.models import Account, OAuthAuthCode, Platform, User, utcnow
from app.routers import auth as auth_router

from .conftest import approve_registration

ROOT_STUDENT_ID = "00000000000"
ROOT_PASSWORD = "devpassword123"
MEMBER_STUDENT_ID = "24125023001"
MEMBER_PASSWORD = "memberpass123"


def _pkce_pair(verifier: str = "a" * 48) -> tuple[str, str]:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'oauth.db'}")
    monkeypatch.setenv("SGU_ACW_COOKIE_SECURE", "false")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", ROOT_STUDENT_ID)
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", ROOT_PASSWORD)

    auth_router.reset_login_rate_limits()
    get_settings.cache_clear()
    db_module.reset_engine()
    db_module.init_db()
    yield
    auth_router.reset_login_rate_limits()
    db_module.reset_engine()
    get_settings.cache_clear()


@pytest.fixture()
def app(env):
    return create_app()


@pytest.fixture()
def root_client(app):
    with TestClient(app) as c:
        resp = c.post(
            "/api/auth/login",
            json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
        )
        assert resp.status_code == 200
        yield c


@pytest.fixture()
def member_client(app, root_client):
    invite = root_client.post("/api/admin/invites", json={"max_uses": 5}).json()["code"]
    with TestClient(app) as c:
        resp = c.post(
            "/api/auth/register",
            json={
                "invite_code": invite,
                "student_id": MEMBER_STUDENT_ID,
                "display_name": "张三",
                "password": MEMBER_PASSWORD,
            },
        )
        assert resp.status_code == 201
        approve_registration(root_client, MEMBER_STUDENT_ID)
        login = c.post(
            "/api/auth/login",
            json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
        )
        assert login.status_code == 200, login.text
        yield c


@pytest.fixture()
def external_client(app, root_client):
    with TestClient(app) as c:
        resp = c.post(
            "/api/auth/register-external",
            json={
                "username": "guest_alice",
                "display_name": "校外访客",
                "password": "guestpassword123",
            },
        )
        assert resp.status_code == 201
        approve_registration(root_client, "guest_alice")
        login = c.post(
            "/api/auth/login",
            json={"student_id": "guest_alice", "password": "guestpassword123"},
        )
        assert login.status_code == 200, login.text
        yield c


def test_desktop_qr_approval_issues_only_one_pkce_authorization_code(app, root_client):
    redirect = "http://127.0.0.1/callback"
    desktop = TestClient(app)
    created = root_client.post(
        "/api/admin/oauth/clients",
        json={
            "name": "校赛客户端",
            "redirect_uris": [redirect],
            "allowed_scopes": ["profile"],
            "is_public": True,
        },
    )
    assert created.status_code == 201, created.text
    client_id = created.json()["client_id"]
    verifier, challenge = _pkce_pair()
    request = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "scope": "profile",
        "code_challenge": challenge,
    }
    with patch("app.routers.auth.qrcode.make") as make_qr:
        make_qr.return_value.to_string.return_value = "<svg />"
        begin = desktop.post("/api/auth/qr-login/begin", json=request)
    assert begin.status_code == 200, begin.text
    scanned = {"token": parse_qs(urlsplit(make_qr.call_args.args[0]).query)["token"][0]}
    poll = {key: begin.json()[key] for key in ("challenge_id", "poll_token")}
    assert desktop.post("/api/auth/qr-login/poll", json=poll).json() == {"status": "pending"}
    assert desktop.post("/api/auth/qr-login/approve", json=scanned).status_code == 401
    assert root_client.post("/api/auth/qr-login/info", json=scanned).json() == {
        "application": "校赛客户端",
        "scopes": ["基本身份信息"],
    }
    assert root_client.post("/api/auth/qr-login/approve", json=scanned).status_code == 200

    barrier = Barrier(2)

    def redeem():
        with TestClient(app, raise_server_exceptions=True) as requester:
            barrier.wait(timeout=5)
            return requester.post("/api/auth/qr-login/poll", json=poll)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: redeem(), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 410]
    approved = next(response for response in responses if response.status_code == 200)
    assert approved.json()["status"] == "authorized"
    assert "set-cookie" not in approved.headers
    token = desktop.post(
        "/api/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": approved.json()["code"],
            "code_verifier": verifier,
            "redirect_uri": redirect,
        },
    )
    assert token.status_code == 200, token.text


def test_admin_oauth_client_crud_and_validation(root_client, member_client):
    # 非管理员不可管理 OAuth 应用
    assert member_client.get("/api/admin/oauth/clients").status_code == 403

    # 非回环 http 回调与含 fragment 的回调均被拒绝
    bad_http = root_client.post(
        "/api/admin/oauth/clients",
        json={
            "name": "Bad",
            "redirect_uris": ["http://example.com/callback"],
        },
    )
    assert bad_http.status_code == 400

    bad_frag = root_client.post(
        "/api/admin/oauth/clients",
        json={
            "name": "BadFrag",
            "redirect_uris": ["https://contest.example.com/cb#hash"],
        },
    )
    assert bad_frag.status_code == 400

    # 创建机密客户端
    created = root_client.post(
        "/api/admin/oauth/clients",
        json={
            "name": "校赛管理服务器",
            "description": "校赛监考后台与客户端身份换取",
            "redirect_uris": [
                "https://contest.sgu-acw.zh314.xyz/oauth/callback",
                "http://127.0.0.1/callback",
            ],
            "allowed_scopes": ["profile", "campus", "nowcoder"],
            "is_public": False,
            "require_campus": True,
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["client_id"].startswith("acw_")
    assert body["client_secret"].startswith("acws_")
    first_secret = body["client_secret"]

    # 列表不泄露密钥
    listed = root_client.get("/api/admin/oauth/clients").json()
    assert len(listed) == 1
    assert "client_secret" not in listed[0]

    # 轮换密钥返回新密钥
    rotated = root_client.post(f"/api/admin/oauth/clients/{body['id']}/rotate-secret")
    assert rotated.status_code == 200
    assert rotated.json()["client_secret"] != first_secret

    # 更新与删除
    patched = root_client.patch(
        f"/api/admin/oauth/clients/{body['id']}",
        json={"name": "校赛服务 v2", "is_active": False},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "校赛服务 v2"
    assert patched.json()["is_active"] is False

    deleted = root_client.delete(f"/api/admin/oauth/clients/{body['id']}")
    assert deleted.status_code == 204
    assert root_client.get("/api/admin/oauth/clients").json() == []


def test_confidential_oauth_flow_with_pkce_and_nowcoder(app, root_client, member_client):
    # 给校内成员绑定一条已验证牛客账号
    with Session(db_module.get_engine()) as db:
        user = db.exec(select(User).where(User.student_id == MEMBER_STUDENT_ID)).one()
        db.add(
            Account(
                user_id=user.id,  # type: ignore[arg-type]
                platform=Platform.NOWCODER,
                identifier="88420123",
                display_name="sgu_zhangsan",
                verified_at=utcnow(),
            )
        )
        db.commit()

    created = root_client.post(
        "/api/admin/oauth/clients",
        json={
            "name": "校赛服务",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "allowed_scopes": ["profile", "campus", "nowcoder"],
            "is_public": False,
            "require_campus": True,
        },
    ).json()
    client_id = created["client_id"]
    client_secret = created["client_secret"]

    verifier, challenge = _pkce_pair("contest-pkce-verifier-0123456789abcdef-XYZ_123")
    loopback_uri = "http://127.0.0.1:49231/callback"

    # 1. 查询授权信息（RFC 8252 loopback 任意端口应匹配）
    info = member_client.get(
        "/api/oauth/authorize/info",
        params={
            "client_id": client_id,
            "redirect_uri": loopback_uri,
            "scope": "profile campus nowcoder",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert info.status_code == 200
    info_data = info.json()
    assert info_data["authenticated"] is True
    assert info_data["eligible"] is True
    assert [s["key"] for s in info_data["scopes"]] == ["profile", "campus", "nowcoder"]

    # 2. 用户拒绝授权应带 error=access_denied & state 返回
    denied = member_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": client_id,
            "redirect_uri": loopback_uri,
            "scope": "profile campus nowcoder",
            "state": "st-deny",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "approve": False,
        },
    )
    assert denied.status_code == 200
    deny_q = parse_qs(urlsplit(denied.json()["redirect_url"]).query)
    assert deny_q["error"] == ["access_denied"]
    assert deny_q["state"] == ["st-deny"]

    # 3. 用户同意授权
    approved = member_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": client_id,
            "redirect_uri": loopback_uri,
            "scope": "profile campus nowcoder",
            "state": "st-ok",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "approve": True,
        },
    )
    assert approved.status_code == 200
    redirect_url = approved.json()["redirect_url"]
    parsed = urlsplit(redirect_url)
    assert parsed.netloc == "127.0.0.1:49231"
    q = parse_qs(parsed.query)
    assert q["state"] == ["st-ok"]
    code = q["code"][0]

    with TestClient(app) as anon:
        # 错误 secret 拒绝
        bad_sec = anon.post(
            "/api/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": "wrong",
                "code": code,
                "redirect_uri": loopback_uri,
                "code_verifier": verifier,
            },
        )
        assert bad_sec.status_code == 401

        # 错误 PKCE verifier 拒绝
        bad_pkce = anon.post(
            "/api/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": loopback_uri,
                "code_verifier": "b" * 48,
            },
        )
        assert bad_pkce.status_code == 400

        # 正常换取（表单提交 + HTTP Basic Auth）
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        exchanged = anon.post(
            "/api/oauth/token",
            headers={"Authorization": f"Basic {basic}"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": loopback_uri,
                "code_verifier": verifier,
            },
        )
        assert exchanged.status_code == 200
        tok = exchanged.json()
        assert tok["token_type"] == "Bearer"
        assert tok["user"]["student_id"] == MEMBER_STUDENT_ID
        assert tok["user"]["display_name"] == "张三"
        assert tok["user"]["is_campus_member"] is True
        assert tok["user"]["nowcoder_uid"] == "88420123"
        assert tok["user"]["nowcoder_verified"] is True

        # 重放同一授权码被拒绝
        replay = anon.post(
            "/api/oauth/token",
            headers={"Authorization": f"Basic {basic}"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": loopback_uri,
                "code_verifier": verifier,
            },
        )
        assert replay.status_code == 400

        # 使用 Bearer 访问 /oauth/userinfo
        me = anon.get(
            "/api/oauth/userinfo",
            headers={"Authorization": f"Bearer {tok['access_token']}"},
        )
        assert me.status_code == 200
        assert me.json()["student_id"] == MEMBER_STUDENT_ID
        assert me.json()["nowcoder_uid"] == "88420123"


def test_public_client_pkce_required_and_campus_gate(
    app, root_client, member_client, external_client
):
    campus_pub = root_client.post(
        "/api/admin/oauth/clients",
        json={
            "name": "校赛桌面客户端",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "is_public": True,
            "require_campus": True,
        },
    ).json()
    assert campus_pub["client_secret"] is None

    # 公开客户端不带 PKCE 被拒
    no_pkce = member_client.get(
        "/api/oauth/authorize/info",
        params={
            "client_id": campus_pub["client_id"],
            "redirect_uri": "http://127.0.0.1:50001/callback",
        },
    )
    assert no_pkce.status_code == 400

    verifier, challenge = _pkce_pair("public-client-verifier-0123456789abcdefghijklmno")

    # 校外用户访问 require_campus=True 应用：info 标记 ineligible，authorize 返回 403
    ext_info = external_client.get(
        "/api/oauth/authorize/info",
        params={
            "client_id": campus_pub["client_id"],
            "redirect_uri": "http://127.0.0.1:50001/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert ext_info.status_code == 200
    assert ext_info.json()["eligible"] is False

    ext_auth = external_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": campus_pub["client_id"],
            "redirect_uri": "http://127.0.0.1:50001/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "approve": True,
        },
    )
    assert ext_auth.status_code == 403

    # 允许校外的公开客户端：校外用户可正常完成 PKCE 换取
    open_pub = root_client.post(
        "/api/admin/oauth/clients",
        json={
            "name": "开放练习客户端",
            "redirect_uris": ["http://localhost/cb"],
            "is_public": True,
            "require_campus": False,
        },
    ).json()
    ok_auth = external_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": open_pub["client_id"],
            "redirect_uri": "http://localhost:50002/cb",
            "scope": "profile campus",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "approve": True,
        },
    )
    assert ok_auth.status_code == 200
    code = parse_qs(urlsplit(ok_auth.json()["redirect_url"]).query)["code"][0]

    with TestClient(app) as anon:
        tok = anon.post(
            "/api/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": open_pub["client_id"],
                "code": code,
                "redirect_uri": "http://localhost:50002/cb",
                "code_verifier": verifier,
            },
        )
        assert tok.status_code == 200
        assert tok.json()["user"]["role"] == "external"
        assert tok.json()["user"]["enroll_year"] is None


def test_expired_auth_code_is_rejected(app, root_client, member_client):
    pub = root_client.post(
        "/api/admin/oauth/clients",
        json={
            "name": "过期测试应用",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "is_public": True,
            "require_campus": True,
        },
    ).json()
    verifier, challenge = _pkce_pair("expired-code-verifier-0123456789abcdefghijklmno")
    approved = member_client.post(
        "/api/oauth/authorize",
        json={
            "client_id": pub["client_id"],
            "redirect_uri": "http://127.0.0.1:50003/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "approve": True,
        },
    )
    code = parse_qs(urlsplit(approved.json()["redirect_url"]).query)["code"][0]

    with Session(db_module.get_engine()) as db:
        row = db.exec(select(OAuthAuthCode)).one()
        row.expires_at = utcnow() - timedelta(seconds=5)
        db.add(row)
        db.commit()

    with TestClient(app) as anon:
        res = anon.post(
            "/api/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": pub["client_id"],
                "code": code,
                "redirect_uri": "http://127.0.0.1:50003/callback",
                "code_verifier": verifier,
            },
        )
        assert res.status_code == 400
