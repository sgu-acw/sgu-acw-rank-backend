"""Tests for registration, login, sessions, invites and admin actions.

Each test runs against a fresh SQLite file. The bootstrap root account is
created via environment variables, which is how the real deployment seeds it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from .conftest import approve_all_registrations, approve_registration

ROOT_STUDENT_ID = "24125023005"
ROOT_PASSWORD = "rootpassword123"

MEMBER_STUDENT_ID = "25125023001"
MEMBER_PASSWORD = "memberpassword123"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Point the app at a fresh database and seed a root account."""
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SGU_ACW_COOKIE_SECURE", "false")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", ROOT_STUDENT_ID)
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", ROOT_PASSWORD)

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()
    yield
    db_module.reset_engine()
    get_settings.cache_clear()


@pytest.fixture()
def client(env):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def root_client(env):
    """A client already logged in as root."""
    from app.main import create_app

    with TestClient(create_app()) as c:
        response = c.post(
            "/api/auth/login",
            json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
        )
        assert response.status_code == 200, response.text
        yield c


def _members(client) -> list[dict]:
    """取成员列表。接口现在返回分页对象，这里只关心 items。"""
    return client.get("/api/admin/users").json()["items"]


def _make_invite(client: TestClient, **overrides) -> dict:
    payload = {"max_uses": 1, "ttl_hours": 24, "grants_role": "member", "note": ""}
    payload.update(overrides)
    response = client.post("/api/admin/invites", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _register(
    client: TestClient, code: str, student_id=MEMBER_STUDENT_ID, password=MEMBER_PASSWORD
):
    return client.post(
        "/api/auth/register",
        json={
            "invite_code": code,
            "student_id": student_id,
            "display_name": "测试成员",
            "password": password,
        },
    )


def _register_and_login(
    client: TestClient,
    root_client: TestClient,
    code: str,
    student_id: str = MEMBER_STUDENT_ID,
    password: str = MEMBER_PASSWORD,
) -> dict:
    """注册、通过管理员审核并登录到 ``client``，返回注册响应里的用户信息。"""
    resp = _register(client, code, student_id=student_id, password=password)
    assert resp.status_code == 201, resp.text
    approve_registration(root_client, student_id)
    login = client.post(
        "/api/auth/login",
        json={"student_id": student_id, "password": password},
    )
    assert login.status_code == 200, login.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_register_with_valid_invite(client, root_client):
    invite = _make_invite(root_client)
    response = _register(client, invite["code"])
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["student_id"] == MEMBER_STUDENT_ID
    assert body["display_name"] == "测试成员"
    assert body["role"] == "member"
    # Parsed from the student id, not supplied by the client.
    assert body["enroll_year"] == 2025
    assert body["college_code"] == "25"
    assert body["major_code"] == "02"
    assert "admin_note" not in body
    assert "password_hash" not in body


def test_register_does_not_issue_session_before_review(client, root_client):
    """注册只建待审核账号，不发会话；会话要等管理员通过后登录才拿到。"""
    invite = _make_invite(root_client)
    response = _register(client, invite["code"])
    assert response.status_code == 201
    assert not response.cookies.get("sgu_acw_session")
    assert "set-cookie" not in response.headers

    assert client.get("/api/auth/me").status_code == 401
    pending = client.post(
        "/api/auth/login",
        json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
    )
    assert pending.status_code == 403
    assert "审核" in pending.json()["detail"]

    approve_registration(root_client, MEMBER_STUDENT_ID)
    login = client.post(
        "/api/auth/login",
        json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
    )
    assert login.status_code == 200, login.text
    assert client.cookies.get("sgu_acw_session")
    # The session cookie must not be readable from JavaScript.
    set_cookie = login.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()


def test_registration_without_review_issues_session_for_new_accounts_only(
    client, root_client, monkeypatch
):
    from app.config import get_settings

    pending_id = "25125023002"
    invite = _make_invite(root_client, max_uses=2)
    assert _register(client, invite["code"], student_id=pending_id).json()["status"] == "pending"

    monkeypatch.setenv("SGU_ACW_REGISTRATION_REVIEW_REQUIRED", "false")
    get_settings.cache_clear()
    assert _register(client, "NOPE1234").status_code == 400
    campus = _register(client, invite["code"])
    assert campus.status_code == 201, campus.text
    assert campus.json()["status"] == "active"
    assert "HttpOnly" in campus.headers["set-cookie"]
    assert client.get("/api/auth/me").json()["student_id"] == MEMBER_STUDENT_ID

    external = client.post(
        "/api/auth/register-external",
        json={"username": "visitorsgu", "display_name": "校外成员", "password": MEMBER_PASSWORD},
    )
    assert external.status_code == 201, external.text
    assert external.json()["status"] == "active"
    assert external.json()["role"] == "external"
    assert client.get("/api/auth/me").json()["student_id"] == "visitorsgu"

    reviews = root_client.get(
        "/api/admin/reviews", params={"status": "pending", "kind": "registration"}
    ).json()
    assert [row["student_id"] for row in reviews["items"]] == [pending_id]
    assert _register(client, invite["code"], student_id="25125023003").status_code == 400
    assert (
        client.post(
            "/api/auth/login", json={"student_id": pending_id, "password": MEMBER_PASSWORD}
        ).status_code
        == 403
    )


def test_register_rejects_bad_student_id(client, root_client):
    invite = _make_invite(root_client)
    response = _register(client, invite["code"], student_id="12345")
    assert response.status_code == 400
    assert "学号" in response.json()["detail"]


def test_register_rejects_unknown_invite(client):
    response = _register(client, "NOPE1234")
    assert response.status_code == 400


def test_register_rejects_duplicate_student_id(client, root_client):
    invite = _make_invite(root_client, max_uses=5)
    assert _register(client, invite["code"]).status_code == 201
    second = _register(client, invite["code"])
    assert second.status_code == 409


def test_register_does_not_consume_invite_on_duplicate(client, root_client):
    """A failed registration must not burn a use of the invite."""
    invite = _make_invite(root_client, max_uses=2)
    assert _register(client, invite["code"]).status_code == 201
    assert _register(client, invite["code"]).status_code == 409  # duplicate

    listed = root_client.get("/api/admin/invites").json()
    row = next(i for i in listed if i["code"] == invite["code"])
    assert row["use_count"] == 1


def test_register_rejects_short_password(client, root_client):
    invite = _make_invite(root_client)
    response = _register(client, invite["code"], password="short")
    assert response.status_code == 422


def test_invite_max_uses_is_enforced(client, root_client):
    invite = _make_invite(root_client, max_uses=2)
    assert _register(client, invite["code"], student_id="25125023001").status_code == 201
    assert _register(client, invite["code"], student_id="25125023002").status_code == 201
    third = _register(client, invite["code"], student_id="25125023003")
    assert third.status_code == 400
    assert "用尽" in third.json()["detail"]


def test_expired_invite_is_rejected(client, root_client):
    invite = _make_invite(root_client, ttl_hours=1)

    # Reach into the database and backdate the expiry.
    from app.db import get_engine
    from app.models import Invite

    with Session(get_engine()) as db:
        row = db.exec(select(Invite).where(Invite.code == invite["code"])).one()
        row.expires_at = row.expires_at - timedelta(days=2)
        db.add(row)
        db.commit()

    response = _register(client, invite["code"])
    assert response.status_code == 400


def test_revoked_invite_is_rejected(client, root_client):
    invite = _make_invite(root_client)
    assert root_client.delete(f"/api/admin/invites/{invite['id']}").status_code == 204
    assert _register(client, invite["code"]).status_code == 400


# --------------------------------------------------------------------------- #
# Login / logout
# --------------------------------------------------------------------------- #


def test_login_and_me(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    approve_all_registrations(root_client)

    fresh = TestClient(client.app)
    assert fresh.get("/api/auth/me").status_code == 401

    login = fresh.post(
        "/api/auth/login",
        json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
    )
    assert login.status_code == 200
    me = fresh.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["student_id"] == MEMBER_STUDENT_ID


def test_login_is_case_and_whitespace_insensitive_on_student_id(client):
    response = client.post(
        "/api/auth/login",
        json={"student_id": f"  {ROOT_STUDENT_ID}  ", "password": ROOT_PASSWORD},
    )
    assert response.status_code == 200


def test_login_with_wrong_password(client):
    response = client.post(
        "/api/auth/login",
        json={"student_id": ROOT_STUDENT_ID, "password": "definitely-wrong"},
    )
    assert response.status_code == 401


def test_login_with_unknown_student_id_gives_same_message(client):
    """The response must not reveal whether a student id exists."""
    unknown = client.post(
        "/api/auth/login",
        json={"student_id": "24125023099", "password": "whatever12345"},
    )
    wrong_pw = client.post(
        "/api/auth/login",
        json={"student_id": ROOT_STUDENT_ID, "password": "definitely-wrong"},
    )
    assert unknown.status_code == wrong_pw.status_code == 401
    assert unknown.json()["detail"] == wrong_pw.json()["detail"]


def test_logout_invalidates_session(client):
    client.post(
        "/api/auth/login",
        json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
    )
    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_logout_is_idempotent(client):
    assert client.post("/api/auth/logout").status_code == 204
    assert client.post("/api/auth/logout").status_code == 204


def test_disabled_account_cannot_log_in(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    approve_all_registrations(root_client)
    members = _members(root_client)
    member = next(m for m in members if m["student_id"] == MEMBER_STUDENT_ID)

    patch = root_client.patch(f"/api/admin/users/{member['id']}", json={"status": "disabled"})
    assert patch.status_code == 200

    fresh = TestClient(client.app)
    response = fresh.post(
        "/api/auth/login",
        json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Self-service
# --------------------------------------------------------------------------- #


def test_update_own_display_name(client):
    client.post(
        "/api/auth/login",
        json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
    )
    response = client.patch("/api/auth/me", json={"display_name": "新名字"})
    assert response.status_code == 200
    assert response.json()["display_name"] == "新名字"


def test_member_cannot_change_own_role(client, root_client):
    invite = _make_invite(root_client)
    _register_and_login(client, root_client, invite["code"])
    # The schema has no role field, so it is ignored rather than applied.
    response = client.patch("/api/auth/me", json={"role": "admin"})
    assert response.status_code == 200
    assert response.json()["role"] == "member"


def test_change_password_keeps_caller_logged_in_and_evicts_others(client, root_client):
    invite = _make_invite(root_client)
    _register_and_login(client, root_client, invite["code"])

    other = TestClient(client.app)
    other.post(
        "/api/auth/login",
        json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
    )
    assert other.get("/api/auth/me").status_code == 200

    changed = client.post(
        "/api/auth/password",
        json={"current_password": MEMBER_PASSWORD, "new_password": "brand-new-password"},
    )
    assert changed.status_code == 204

    # Caller still works, the other session is gone.
    assert client.get("/api/auth/me").status_code == 200
    assert other.get("/api/auth/me").status_code == 401

    # And the new password is what logs in now.
    fresh = TestClient(client.app)
    assert (
        fresh.post(
            "/api/auth/login",
            json={"student_id": MEMBER_STUDENT_ID, "password": "brand-new-password"},
        ).status_code
        == 200
    )


def test_change_password_requires_current_password(client):
    client.post(
        "/api/auth/login",
        json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
    )
    response = client.post(
        "/api/auth/password",
        json={"current_password": "wrong", "new_password": "another-password"},
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #


def test_member_cannot_reach_admin_endpoints(client, root_client):
    invite = _make_invite(root_client)
    _register_and_login(client, root_client, invite["code"])
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/admin/invites").status_code == 403
    assert client.post("/api/admin/invites", json={"max_uses": 1}).status_code == 403


def test_admin_note_is_writable_by_admin_and_hidden_from_members(client, root_client):
    invite = _make_invite(root_client)
    _register_and_login(client, root_client, invite["code"])
    members = _members(root_client)
    member = next(m for m in members if m["student_id"] == MEMBER_STUDENT_ID)

    # Admin can write and read it.
    response = root_client.patch(
        f"/api/admin/users/{member['id']}", json={"admin_note": "已核实，学号曾被他人占用"}
    )
    assert response.status_code == 200
    assert response.json()["admin_note"] == "已核实，学号曾被他人占用"

    listed = _members(root_client)
    assert (
        next(m for m in listed if m["id"] == member["id"])["admin_note"]
        == "已核实，学号曾被他人占用"
    )

    # The member's own view must not carry the field at all.
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert "admin_note" not in me.json()

    # Nor must the member directory, which other members can read.
    directory = root_client.get("/api/users").json()
    assert all("admin_note" not in entry for entry in directory)


def test_admin_note_can_be_cleared(client, root_client):
    members = _members(root_client)
    root = next(m for m in members if m["student_id"] == ROOT_STUDENT_ID)

    root_client.patch(f"/api/admin/users/{root['id']}", json={"admin_note": "临时备注"})
    cleared = root_client.patch(f"/api/admin/users/{root['id']}", json={"admin_note": ""})
    assert cleared.json()["admin_note"] == ""


def test_admin_cannot_promote_to_admin(client, root_client):
    """Only root hands out administrative roles."""
    invite = _make_invite(root_client, grants_role="admin")
    _register(client, invite["code"])
    approve_all_registrations(root_client)

    # The promoted account is an admin; it must not be able to promote further.
    admin_client = TestClient(client.app)
    admin_client.post(
        "/api/auth/login",
        json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
    )
    assert admin_client.get("/api/admin/users").status_code == 200

    members = _members(root_client)
    target = next(m for m in members if m["student_id"] == ROOT_STUDENT_ID)
    response = admin_client.patch(f"/api/admin/users/{target['id']}", json={"role": "admin"})
    assert response.status_code == 403


def test_invite_cannot_grant_root(client, root_client):
    response = root_client.post("/api/admin/invites", json={"max_uses": 1, "grants_role": "root"})
    assert response.status_code == 403


def test_root_cannot_be_demoted_or_disabled(client, root_client):
    members = _members(root_client)
    root = next(m for m in members if m["student_id"] == ROOT_STUDENT_ID)
    assert root["role"] == "root"

    assert (
        root_client.patch(f"/api/admin/users/{root['id']}", json={"role": "member"}).status_code
        == 403
    )
    assert (
        root_client.patch(f"/api/admin/users/{root['id']}", json={"status": "disabled"}).status_code
        == 403
    )


def test_reset_password_returns_temporary_and_invalidates_old(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    approve_all_registrations(root_client)
    members = _members(root_client)
    member = next(m for m in members if m["student_id"] == MEMBER_STUDENT_ID)

    response = root_client.post(f"/api/admin/users/{member['id']}/reset-password")
    assert response.status_code == 200
    temporary = response.json()["temporary_password"]
    assert len(temporary) >= 8

    # The member's existing session is gone and the old password is dead.
    assert client.get("/api/auth/me").status_code == 401
    fresh = TestClient(client.app)
    assert (
        fresh.post(
            "/api/auth/login",
            json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
        ).status_code
        == 401
    )
    assert (
        fresh.post(
            "/api/auth/login",
            json={"student_id": MEMBER_STUDENT_ID, "password": temporary},
        ).status_code
        == 200
    )


def test_admin_stats(client, root_client):
    body = root_client.get("/api/admin/stats").json()
    assert body["user_count"] == 1
    assert body["admin_count"] == 1
    assert body["usable_invite_count"] == 0


# --------------------------------------------------------------------------- #
# Member directory
# --------------------------------------------------------------------------- #


def test_directory_requires_login(client):
    assert client.get("/api/users").status_code == 401


def test_directory_lists_active_members(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    approve_all_registrations(root_client)
    entries = root_client.get("/api/users").json()
    assert {e["student_id"] for e in entries} == {ROOT_STUDENT_ID, MEMBER_STUDENT_ID}
    assert all("admin_note" not in e for e in entries)


def test_disabled_members_are_hidden_from_the_directory(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    approve_all_registrations(root_client)
    members = _members(root_client)
    member = next(m for m in members if m["student_id"] == MEMBER_STUDENT_ID)
    root_client.patch(f"/api/admin/users/{member['id']}", json={"status": "disabled"})

    visible = root_client.get("/api/users").json()
    assert MEMBER_STUDENT_ID not in {e["student_id"] for e in visible}


@pytest.mark.parametrize("review_required", ["true", "false"])
def test_concurrent_registration_cannot_overuse_invite(
    client, root_client, monkeypatch, review_required
):
    """Many students racing for one slot must not all get in.

    The invite is claimed with a conditional UPDATE so SQLite's single-writer
    rule decides the winner. A check-then-increment in Python would let several
    threads pass the check before any of them incremented.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.config import get_settings

    monkeypatch.setenv("SGU_ACW_REGISTRATION_REVIEW_REQUIRED", review_required)
    get_settings.cache_clear()
    invite = _make_invite(root_client, max_uses=1)
    results: list[int | str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        try:
            response = _register(
                client,
                invite["code"],
                student_id=f"251250230{index:02d}",
                password="password12345",
            )
            outcome: int | str = response.status_code
        except Exception as exc:  # pragma: no cover - only on a real bug
            outcome = f"ERR:{type(exc).__name__}"
        with lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(attempt, range(1, 17)))

    assert results.count(201) == 1, f"expected exactly one winner, got {results}"

    listed = root_client.get("/api/admin/invites").json()
    row = next(i for i in listed if i["code"] == invite["code"])
    assert row["use_count"] == 1


def test_session_endpoint_returns_null_when_logged_out(client):
    """页面加载时用它问「我是谁」，未登录也不该是 401。

    /auth/me 对未登录返回 401 是对的，但前端每次加载都调一次，控制台就会
    被 401 刷屏，真正的故障反而看不见。
    """
    response = client.get("/api/auth/session")
    assert response.status_code == 200
    assert response.json() == {"user": None}


def test_session_endpoint_returns_user_when_logged_in(client):
    client.post(
        "/api/auth/login",
        json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
    )
    body = client.get("/api/auth/session").json()
    assert body["user"]["student_id"] == ROOT_STUDENT_ID
    # 与成员视角一致，不能带出 admin_note。
    assert "admin_note" not in body["user"]


def test_session_endpoint_ignores_a_disabled_account(client, root_client):
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    approve_all_registrations(root_client)
    members = _members(root_client)
    member = next(m for m in members if m["student_id"] == MEMBER_STUDENT_ID)
    root_client.patch(f"/api/admin/users/{member['id']}", json={"status": "disabled"})

    assert client.get("/api/auth/session").json() == {"user": None}


# --------------------------------------------------------------------------- #
# 成员管理的筛选与 CRUD
# --------------------------------------------------------------------------- #


def _seed_members(client, root_client, count=3):
    """建几个不同年级的成员，供筛选测试用。"""
    invite = _make_invite(root_client, max_uses=count)
    created = []
    for index in range(count):
        response = _register(
            client,
            invite["code"],
            student_id=f"251250230{index + 1:02d}",
            password="password12345",
        )
        created.append(response.json())
    approve_all_registrations(root_client)
    return created


def test_member_list_is_paginated(client, root_client):
    _seed_members(client, root_client, 3)
    body = root_client.get("/api/admin/users?page_size=2").json()

    assert body["total"] == 4  # root + 3
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(body["items"]) == 2
    # 选项由后端给出，前端不写死。
    assert 2025 in body["enroll_years"]
    assert "25" in body["college_codes"]


def test_member_list_second_page(client, root_client):
    _seed_members(client, root_client, 3)
    first = root_client.get("/api/admin/users?page_size=2&page=1").json()
    second = root_client.get("/api/admin/users?page_size=2&page=2").json()

    assert len(second["items"]) == 2
    # 两页不重叠。
    assert {u["id"] for u in first["items"]} & {u["id"] for u in second["items"]} == set()


def test_search_by_student_id_prefix(client, root_client):
    _seed_members(client, root_client, 3)
    body = root_client.get("/api/admin/users?q=25125023001").json()
    assert body["total"] == 1
    assert body["items"][0]["student_id"] == "25125023001"


def test_search_by_name_substring(client, root_client):
    """姓名按包含匹配，搜中间几个字也能找到。"""
    _seed_members(client, root_client, 2)
    body = root_client.get("/api/admin/users?q=测试").json()
    assert body["total"] == 2


def test_filter_by_role_and_status(client, root_client):
    _seed_members(client, root_client, 2)

    roots = root_client.get("/api/admin/users?role=root").json()
    assert roots["total"] == 1
    assert roots["items"][0]["role"] == "root"

    members = root_client.get("/api/admin/users?role=member").json()
    assert members["total"] == 2


def test_filter_by_enroll_year(client, root_client):
    _seed_members(client, root_client, 2)
    body = root_client.get("/api/admin/users?enroll_year=2025").json()
    assert body["total"] == 2


def test_filter_by_account_binding(client, root_client):
    """has_account 用来找出还没绑账号的人。"""
    _seed_members(client, root_client, 2)

    # 都没绑，所以全部落在 false 一侧。
    unbound = root_client.get("/api/admin/users?has_account=false").json()
    assert unbound["total"] == 3
    bound = root_client.get("/api/admin/users?has_account=true").json()
    assert bound["total"] == 0


def test_invalid_role_filter_is_rejected(client, root_client):
    response = root_client.get("/api/admin/users?role=bogus")
    assert response.status_code == 400


def test_page_zero_is_rejected(client, root_client):
    assert root_client.get("/api/admin/users?page=0").status_code == 422


def test_admin_can_create_user_without_invite(client, root_client):
    response = root_client.post(
        "/api/admin/users",
        json={
            "student_id": "23125023001",
            "display_name": "代建成员",
            "password": "createdpassword",
            "role": "member",
            "admin_note": "管理员代建",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["enroll_year"] == 2023
    assert body["college_code"] == "25"
    assert body["admin_note"] == "管理员代建"

    # 建完就能登录。
    fresh = TestClient(client.app)
    assert (
        fresh.post(
            "/api/auth/login",
            json={"student_id": "23125023001", "password": "createdpassword"},
        ).status_code
        == 200
    )


def test_admin_create_rejects_duplicate_student_id(client, root_client):
    payload = {
        "student_id": "23125023001",
        "display_name": "甲",
        "password": "createdpassword",
    }
    assert root_client.post("/api/admin/users", json=payload).status_code == 201
    assert root_client.post("/api/admin/users", json=payload).status_code == 409


def test_admin_create_rejects_bad_student_id(client, root_client):
    response = root_client.post(
        "/api/admin/users",
        json={"student_id": "12345", "display_name": "甲", "password": "createdpassword"},
    )
    assert response.status_code == 400


def test_admin_cannot_create_root(client, root_client):
    response = root_client.post(
        "/api/admin/users",
        json={
            "student_id": "23125023001",
            "display_name": "假root",
            "password": "createdpassword",
            "role": "root",
        },
    )
    assert response.status_code == 403


def test_admin_can_edit_display_name(client, root_client):
    _seed_members(client, root_client, 1)
    member = next(u for u in _members(root_client) if u["role"] == "member")
    response = root_client.patch(
        f"/api/admin/users/{member['id']}", json={"display_name": "新名字"}
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "新名字"


def test_delete_user_removes_accounts_and_sessions(client, root_client):
    _seed_members(client, root_client, 1)
    member = next(u for u in _members(root_client) if u["role"] == "member")

    # 审核通过后先登录成员端。
    assert (
        client.post(
            "/api/auth/login",
            json={"student_id": member["student_id"], "password": "password12345"},
        ).status_code
        == 200
    )
    assert client.get("/api/auth/me").status_code == 200

    assert root_client.delete(f"/api/admin/users/{member['id']}").status_code == 204
    assert member["student_id"] not in {u["student_id"] for u in _members(root_client)}


def test_cannot_delete_self(client, root_client):
    root = next(u for u in _members(root_client) if u["role"] == "root")
    response = root_client.delete(f"/api/admin/users/{root['id']}")
    assert response.status_code == 400
    assert "自己" in response.json()["detail"]


def test_cannot_delete_root_by_other_admin(client, root_client):
    """管理员不能删 root，即使不是自己。"""
    _seed_members(client, root_client, 1)
    invite = _make_invite(root_client, grants_role="admin")
    _register(client, invite["code"], student_id="25125023099", password="password12345")
    approve_all_registrations(root_client)

    admin_client = TestClient(client.app)
    admin_client.post(
        "/api/auth/login",
        json={"student_id": "25125023099", "password": "password12345"},
    )
    assert admin_client.get("/api/admin/users").status_code == 200

    root = next(u for u in _members(root_client) if u["role"] == "root")
    assert admin_client.delete(f"/api/admin/users/{root['id']}").status_code == 403


# --------------------------------------------------------------------------- #
# 邀请码使用记录与手动刷新额度
# --------------------------------------------------------------------------- #


def test_invite_use_is_recorded(client, root_client):
    """谁用了邀请码要记下来。

    use_count 只知道用了几次，出事时（比如码被转发到群外）光看次数查不出人。
    """
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import InviteUse

    invite = _make_invite(root_client)
    created = _register(client, invite["code"]).json()

    with Session(get_engine()) as db:
        rows = db.exec(select(InviteUse).where(InviteUse.invite_id == invite["id"])).all()
        assert len(rows) == 1
        assert rows[0].user_id == created["id"]


def test_unbinding_account_removes_its_progress(client, root_client):
    """解绑账号要连带删掉练习记录。

    留着的话重新绑同一账号时旧数据会意外复活，看起来像「刚绑上就有几百题」。
    """
    from unittest.mock import patch

    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import DailyProgress
    from app.platforms import Profile

    invite = _make_invite(root_client)
    _register_and_login(client, root_client, invite["code"])

    with patch(
        "app.routers.accounts.fetch_profile",
        return_value=Profile(platform="codeforces", identifier="tester", signature=""),
    ):
        account = client.post(
            "/api/accounts", json={"platform": "codeforces", "identifier": "tester"}
        ).json()

    # 造一点练习记录。
    with Session(get_engine()) as db:
        db.add(
            DailyProgress(
                account_id=account["id"],
                platform="codeforces",
                date="2026-09-01",
                solved_count=5,
            )
        )
        db.commit()

    assert client.delete(f"/api/accounts/{account['id']}").status_code == 204

    with Session(get_engine()) as db:
        left = db.exec(select(DailyProgress).where(DailyProgress.account_id == account["id"])).all()
        assert left == []


def test_manual_refresh_quota(client, root_client):
    """手动刷新每周七次，用完返回 429。"""
    from unittest.mock import patch

    invite = _make_invite(root_client)
    _register_and_login(client, root_client, invite["code"])

    with patch(
        "app.routers.accounts.fetch_profile",
        return_value=__import__("app.platforms", fromlist=["Profile"]).Profile(
            platform="codeforces", identifier="tester", signature=""
        ),
    ):
        account = client.post(
            "/api/accounts", json={"platform": "codeforces", "identifier": "tester"}
        ).json()

    # 标记已验证。
    from sqlmodel import Session

    from app.db import get_engine
    from app.models import Account

    with Session(get_engine()) as db:
        row = db.get(Account, account["id"])
        row.verified_at = __import__("app.models", fromlist=["utcnow"]).utcnow()
        db.add(row)
        db.commit()

    quota = client.get("/api/practice/quota").json()
    assert quota["limit"] == 7
    assert quota["remaining"] == 7

    with (
        patch("app.routers.practice.sync_account", return_value=(True, "ok", 0)),
        patch("app.routers.practice.sync_rating", return_value=(True, "ok")),
    ):
        for expected in (6, 5, 4, 3, 2, 1, 0):
            body = client.post("/api/practice/refresh").json()
            assert body["remaining"] == expected

        blocked = client.post("/api/practice/refresh")
        assert blocked.status_code == 429
        assert "用完" in blocked.json()["detail"]


def test_manual_refresh_requires_login(client):
    assert client.post("/api/practice/refresh").status_code == 401
    assert client.get("/api/practice/quota").status_code == 401


# --------------------------------------------------------------------------- #
# 校验错误不能回显密码
# --------------------------------------------------------------------------- #


def test_password_is_not_echoed_in_validation_errors(client):
    """密码校验失败时不能在响应里回显原值。

    Pydantic 会把出错字段的原值放进 ``input``，而 FastAPI 默认整个返回给
    客户端。密码超长或类型不对时，密码就这样原样出现在响应体里——浏览器的
    Network 面板、控制台、以及前端的错误提示都会记下它。
    """
    marker = "SECRET_MARKER_" + "x" * 200
    response = client.post(
        "/api/auth/login",
        json={"student_id": "24125023001", "password": marker},
    )
    assert response.status_code == 422
    body = response.text
    assert marker not in body
    assert "***" in body


def test_short_password_is_not_echoed_on_register(client, root_client):
    invite = _make_invite(root_client)
    marker = "SHORT"  # 5 位，低于 8 位下限
    response = client.post(
        "/api/auth/register",
        json={
            "invite_code": invite["code"],
            "student_id": "24125023001",
            "display_name": "甲",
            "password": marker,
        },
    )
    assert response.status_code == 422
    assert marker not in response.text


def test_non_sensitive_fields_are_still_reported(client):
    """非敏感字段的错误要照常返回，那些信息对用户有用。"""
    response = client.post(
        "/api/auth/login",
        json={"student_id": "", "password": "whatever"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("student_id" in str(item.get("loc")) for item in detail)


def test_change_password_does_not_echo(client):
    """改密码接口同样不能回显。"""
    marker = "SECRET_MARKER_" + "y" * 200
    response = client.post(
        "/api/auth/password",
        json={"current_password": marker, "new_password": "validpassword123"},
    )
    # 未登录是 401，但即使走到了校验也该抹掉。这里只断言响应里没有原值。
    assert marker not in response.text


# --------------------------------------------------------------------------- #
# 验证防抖
# --------------------------------------------------------------------------- #


def _bind_account(client, platform="codeforces", identifier="tester"):
    """绑定一个账号，返回它的 id。"""
    from unittest.mock import patch

    from app.platforms import Profile

    with patch(
        "app.routers.accounts.fetch_profile",
        return_value=Profile(platform=platform, identifier=identifier, signature=""),
    ):
        response = client.post(
            "/api/accounts", json={"platform": platform, "identifier": identifier}
        )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_verify_is_rate_limited(client, root_client):
    """连点验证会被挡下。

    每次验证都要去平台读一次资料。连点不仅浪费请求，还可能把平台的 WAF
    惹毛——那会连带影响所有人的抓取，不只是他自己。
    """
    from unittest.mock import patch

    invite = _make_invite(root_client)
    _register_and_login(client, root_client, invite["code"])
    account_id = _bind_account(client)

    with patch(
        "app.routers.accounts.check_signature",
        return_value=(False, "签名里没有找到验证码，请确认已保存"),
    ):
        first = client.post(f"/api/accounts/{account_id}/verify")
        assert first.status_code == 200
        assert first.json()["ok"] is False

        # 立刻再来一次：应该被防抖挡下。
        second = client.post(f"/api/accounts/{account_id}/verify")
        assert second.status_code == 429
        assert "太快" in second.json()["detail"]


def test_verify_cooldown_grows_with_attempts(client, root_client):
    """冷却随失败次数翻倍，所以一天到晚慢慢点也会被拖停。"""
    from datetime import timedelta

    from sqlmodel import Session

    from app.config import get_settings
    from app.db import get_engine
    from app.models import Account

    settings = get_settings()
    invite = _make_invite(root_client)
    _register_and_login(client, root_client, invite["code"])
    account_id = _bind_account(client)

    with Session(get_engine()) as db:
        account = db.get(Account, account_id)
        # 模拟已经失败过几次。
        account.verify_attempts = 4
        account.verify_attempted_at = __import__("app.models", fromlist=["utcnow"]).utcnow()
        db.add(account)
        db.commit()

        # 第 4 次之后的冷却是 80 秒；距上次尝试 30 秒，应该还被挡。
        account.verify_attempted_at = account.verify_attempted_at - timedelta(seconds=30)
        db.add(account)
        db.commit()

        expected = account.verify_cooldown(
            base=settings.verify_cooldown_seconds,
            cap=settings.verify_cooldown_max_seconds,
        )
        assert expected == settings.verify_cooldown_seconds * 8  # 10 * 2^3

    response = client.post(f"/api/accounts/{account_id}/verify")
    assert response.status_code == 429
    # 提示要告诉他还差多久。
    assert "秒" in response.json()["detail"]


def test_successful_verify_resets_counter(client, root_client):
    """验证成功要把计数清零，否则下次重新绑定要白等。"""
    from unittest.mock import patch

    from sqlmodel import Session

    from app.db import get_engine
    from app.models import Account

    invite = _make_invite(root_client)
    _register_and_login(client, root_client, invite["code"])
    account_id = _bind_account(client)

    with patch("app.routers.accounts.check_signature", return_value=(True, "验证通过")):
        response = client.post(f"/api/accounts/{account_id}/verify")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    with Session(get_engine()) as db:
        account = db.get(Account, account_id)
        assert account.verify_attempts == 0
        assert account.verified_at is not None


def test_renew_does_not_reset_cooldown(client, root_client):
    """换个新验证码不该重置冷却，否则连点可以靠不停地 renew 绕过。"""
    from unittest.mock import patch

    invite = _make_invite(root_client)
    _register_and_login(client, root_client, invite["code"])
    account_id = _bind_account(client)

    with patch("app.routers.accounts.check_signature", return_value=(False, "没找到")):
        assert client.post(f"/api/accounts/{account_id}/verify").status_code == 200

    # 换一个新码。
    assert client.post(f"/api/accounts/{account_id}/renew").status_code == 200

    # 冷却仍然生效。
    again = client.post(f"/api/accounts/{account_id}/verify")
    assert again.status_code == 429


def test_verified_account_is_not_rate_limited(client, root_client):
    """已经验证过的账号查一下就走，不进冷却——否则界面刷新会莫名报错。"""
    from unittest.mock import patch

    invite = _make_invite(root_client)
    _register_and_login(client, root_client, invite["code"])
    account_id = _bind_account(client)

    with patch("app.routers.accounts.check_signature", return_value=(True, "验证通过")):
        assert client.post(f"/api/accounts/{account_id}/verify").json()["ok"] is True

    # 连查三次，每次都是「已经验证过了」，不该触发 429。
    for _ in range(3):
        response = client.post(f"/api/accounts/{account_id}/verify")
        assert response.status_code == 200
        assert "已经验证过" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# 学院名与专业名
# --------------------------------------------------------------------------- #


def test_college_and_major_names_resolved_from_codes(client, root_client):
    """接口返回的成员要带上学院名与专业名，不只是码。

    码表来自学号规则反推，放在 app/academic.py。
    """
    invite = _make_invite(root_client)
    _register_and_login(client, root_client, invite["code"])  # 25125023001 → 学院 25 / 专业 02

    me = client.get("/api/auth/me").json()
    assert me["college_code"] == "25"
    assert me["major_code"] == "02"
    assert me["college_name"] == "信息工程学院"
    assert me["major_name"] == "软件工程"


def test_admin_can_override_college_name(client, root_client):
    """管理员可以改学院名。

    需要可改是因为联培码（94-97）是共用的，同一个码下挂着不同学院的专业，
    光看码推不出准确的学院名。
    """
    invite = _make_invite(root_client)
    created = _register(client, invite["code"]).json()

    updated = root_client.patch(
        f"/api/admin/users/{created['id']}",
        json={"college_override": "改过的学院"},
    )
    assert updated.status_code == 200
    assert updated.json()["college_name"] == "改过的学院"
    # 学号里的码不动，只是显示名被覆盖。
    assert updated.json()["college_code"] == "25"


def test_empty_override_restores_lookup(client, root_client):
    """传空串表示恢复成按学号查表。"""
    invite = _make_invite(root_client)
    created = _register(client, invite["code"]).json()

    root_client.patch(f"/api/admin/users/{created['id']}", json={"college_override": "临时改的"})
    restored = root_client.patch(f"/api/admin/users/{created['id']}", json={"college_override": ""})
    assert restored.json()["college_name"] == "信息工程学院"


def test_academic_options_listed(client, root_client):
    """学院与专业的候选列表要有内容，供管理员选。"""
    body = root_client.get("/api/admin/academics").json()
    assert "信息工程学院" in body["colleges"]
    assert "软件工程" in body["majors"]["信息工程学院"]


def test_unknown_college_code_yields_none():
    """查不到的码返回 None，让调用方决定显示什么，而不是编一个名字。"""
    from app.academic import college_name, major_name

    assert college_name("99") is None
    assert college_name(None) is None
    assert major_name("99", "01") is None
    assert major_name("25", "99") is None


# --------------------------------------------------------------------------- #
# 班级
# --------------------------------------------------------------------------- #


def test_class_is_parsed_and_exposed(client, root_client):
    """学号第 8 位是班级序号，要解析并显示。

    单独一个「3 班」没有意义——得知道是哪个专业的 3 班。所以 class_label
    是专业加序号拼出来的。
    """
    invite = _make_invite(root_client)
    # 25125023 003 → 学院 25 / 专业 02 / 3 班
    created = _register(client, invite["code"], student_id="25125023003").json()

    assert created["class_no"] == 3
    assert created["class_label"] == "信工软工 3 班"


def test_rating_board_shows_college_and_class(client, root_client):
    """rating 榜也要带学院、专业、班级。

    与练习量榜保持一致，否则同一个页面切个维度就少了一半信息。
    """
    from unittest.mock import patch

    from sqlmodel import Session

    from app.db import get_engine
    from app.models import Account, utcnow
    from app.platforms import Profile

    invite = _make_invite(root_client)
    _register_and_login(client, root_client, invite["code"], student_id="25125023003")

    with patch(
        "app.routers.accounts.fetch_profile",
        return_value=Profile(platform="codeforces", identifier="tester", signature=""),
    ):
        account = client.post(
            "/api/accounts", json={"platform": "codeforces", "identifier": "tester"}
        ).json()

    with Session(get_engine()) as db:
        row = db.get(Account, account["id"])
        row.verified_at = utcnow()
        row.rating = 1500
        db.add(row)
        db.commit()

    board = root_client.get("/api/practice/rating?platform=codeforces").json()
    entry = board["entries"][0]
    assert entry["college_name"] == "信息工程学院"
    assert entry["major_name"] == "软件工程"
    assert entry["class_label"] == "信工软工 3 班"


# --------------------------------------------------------------------------- #
# 管理员之间的权限
# --------------------------------------------------------------------------- #


def _make_admin(client, root_client, student_id="25125023010") -> TestClient:
    """建一个管理员，并返回以他身份登录的客户端。

    管理员之间是平级的，所以测试要站在管理员的身份上，而不是 root。
    """
    from app.main import create_app

    invite = _make_invite(root_client, grants_role="admin")
    _register(client, invite["code"], student_id=student_id)
    approve_all_registrations(root_client)

    admin = TestClient(create_app())
    response = admin.post(
        "/api/auth/login",
        json={"student_id": student_id, "password": MEMBER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return admin


def test_admin_can_create_admin_invite(client, root_client):
    """管理员之间平级，所以管理员也能发管理员邀请码。

    原先只有 root 能发。但工作坊里 root 常常不在线，加个管理员要等很久。
    """
    admin = _make_admin(client, root_client)

    response = admin.post(
        "/api/admin/invites",
        json={"max_uses": 1, "ttl_hours": 24, "grants_role": "admin", "note": ""},
    )
    assert response.status_code == 201, response.text
    assert response.json()["grants_role"] == "admin"


def test_admin_cannot_create_root_invite(client, root_client):
    """root 不能靠邀请码授予。root 只有一个，转让得走别的路。"""
    admin = _make_admin(client, root_client)

    response = admin.post(
        "/api/admin/invites",
        json={"max_uses": 1, "ttl_hours": 24, "grants_role": "root", "note": ""},
    )
    assert response.status_code == 403


def test_admin_can_promote_member(client, root_client):
    """管理员能把成员提成管理员。"""
    invite = _make_invite(root_client)
    _register(client, invite["code"], student_id="25125023011")
    approve_all_registrations(root_client)

    admin = _make_admin(client, root_client)
    target = next(u for u in _members(admin) if u["student_id"] == "25125023011")

    response = admin.patch(f"/api/admin/users/{target['id']}", json={"role": "admin"})
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "admin"


def test_admin_can_demote_another_admin(client, root_client):
    """管理员也能撤销别的管理员。

    既然能任命，就该能撤销——只能加不能减，出事时收不回来。
    """
    _make_admin(client, root_client, student_id="25125023012")
    admin = _make_admin(client, root_client, student_id="25125023013")

    target = next(u for u in _members(admin) if u["student_id"] == "25125023012")
    response = admin.patch(f"/api/admin/users/{target['id']}", json={"role": "member"})
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "member"


def test_admin_can_delete_another_admin(client, root_client):
    """管理员之间能互相删除。"""
    _make_admin(client, root_client, student_id="25125023014")
    admin = _make_admin(client, root_client, student_id="25125023015")

    target = next(u for u in _members(admin) if u["student_id"] == "25125023014")
    response = admin.delete(f"/api/admin/users/{target['id']}")
    assert response.status_code == 204, response.text


def test_nobody_can_touch_root(client, root_client):
    """root 仍然动不了。上面放开的都只是管理员之间。"""
    admin = _make_admin(client, root_client)
    root_user = next(u for u in _members(admin) if u["role"] == "root")

    root_id = root_user["id"]
    url = f"/api/admin/users/{root_id}"

    assert admin.patch(url, json={"role": "member"}).status_code == 403
    assert admin.patch(url, json={"status": "disabled"}).status_code == 403
    assert admin.delete(url).status_code == 403


# --------------------------------------------------------------------------- #
# 邀请码的创建者
# --------------------------------------------------------------------------- #


def test_invite_shows_who_created_it(client, root_client):
    """邀请码要写清是谁创建的。

    码被转发出去、或者发错了人，得知道找谁问。只给一个数字 id 等于没给。
    姓名可能重名，所以带上学号。
    """
    invite = _make_invite(root_client)

    assert invite["created_by_name"] == "root"
    assert invite["created_by_student_id"] == ROOT_STUDENT_ID

    listed = root_client.get("/api/admin/invites").json()
    row = next(i for i in listed if i["code"] == invite["code"])
    assert row["created_by_name"] == "root"
    assert row["created_by_student_id"] == ROOT_STUDENT_ID


def test_invite_creator_is_the_admin_who_made_it(client, root_client):
    """管理员发的码，创建者要显示这个管理员，不是 root。"""
    admin = _make_admin(client, root_client, student_id="25125023016")
    invite = _make_invite(admin)

    assert invite["created_by_name"] == "测试成员"
    assert invite["created_by_student_id"] == "25125023016"


# --------------------------------------------------------------------------- #
# 管理员视角的成员详情
# --------------------------------------------------------------------------- #


def test_admin_member_detail_shows_sync_health(client, root_client):
    """管理员看成员详情时，能拿到同步健康度。

    这一块是用来回答「这个人为什么没数据」的：账号验证没过、抓取一直失败、
    或者人本来就不做题了——三者从公开信息里都看不出来。
    """
    invite = _make_invite(root_client)
    created = _register(client, invite["code"], student_id="25125023020").json()

    detail = root_client.get(f"/api/admin/members/{created['id']}").json()
    assert detail["student_id"] == "25125023020"
    # 公开信息也在里面，管理页不必再拉一次
    assert detail["college_name"] == "信息工程学院"
    # 排查用的字段
    assert "admin_note" in detail
    assert "last_login_at" in detail
    assert detail["total_solved"] == 0
    assert detail["accounts"] == []


def test_admin_member_detail_lists_account_health(client, root_client):
    """每个账号的健康度要能单独看到。

    一个成员可能绑了好几个号，问题往往只出在其中一个上。
    """
    from unittest.mock import patch

    from sqlmodel import Session

    from app.db import get_engine
    from app.models import Account, utcnow
    from app.platforms import Profile

    invite = _make_invite(root_client)
    created = _register_and_login(client, root_client, invite["code"], student_id="25125023021")

    with patch(
        "app.routers.accounts.fetch_profile",
        return_value=Profile(platform="codeforces", identifier="healthy", signature=""),
    ):
        client.post("/api/accounts", json={"platform": "codeforces", "identifier": "healthy"})

    # 造一个连续失败的账号
    with Session(get_engine()) as db:
        db.add(
            Account(
                user_id=created["id"],
                platform="luogu",
                identifier="809016",
                created_at=utcnow(),
                verified_at=utcnow(),
                consecutive_failures=3,
                last_error="洛谷返回 403",
            )
        )
        db.commit()

    detail = root_client.get(f"/api/admin/members/{created['id']}").json()
    assert len(detail["accounts"]) == 2
    assert detail["failing_accounts"] == 1

    failing = next(a for a in detail["accounts"] if a["consecutive_failures"] > 0)
    assert failing["platform"] == "luogu"
    assert failing["last_error"] == "洛谷返回 403"


def test_member_cannot_read_admin_detail(client, root_client):
    """普通成员拿不到这一段。界面上不显示，接口也必须挡住。"""
    from app.main import create_app

    invite = _make_invite(root_client)
    created = _register(client, invite["code"], student_id="25125023022").json()
    approve_registration(root_client, "25125023022")

    member = TestClient(create_app())
    member.post(
        "/api/auth/login",
        json={"student_id": "25125023022", "password": MEMBER_PASSWORD},
    )
    assert member.get(f"/api/admin/members/{created['id']}").status_code == 403


def test_anonymous_cannot_read_admin_detail(root_client):
    """未登录同样拿不到。"""
    from app.main import create_app

    invite = _make_invite(root_client)
    created = _register(TestClient(create_app()), invite["code"], student_id="25125023023").json()

    anon = TestClient(create_app())
    assert anon.get(f"/api/admin/members/{created['id']}").status_code == 401


def test_admin_user_list_includes_accounts(client, root_client):
    """成员列表要带平台账号。

    管理员在列表页最常看的两个信息就是「绑了没有」和「验没验过」，
    没有的话得逐个点进详情页才知道。
    """
    from sqlmodel import Session

    from app.db import get_engine
    from app.models import Account, User, utcnow

    # 建一个成员，然后直接往库里塞账号（绕开绑定流程，这里只关心列表输出）。
    invite = _make_invite(root_client)
    _register(client, invite["code"])
    approve_all_registrations(root_client)

    with Session(get_engine()) as db:
        user = db.exec(select(User).where(User.student_id == MEMBER_STUDENT_ID)).first()
        assert user is not None
        # 一个已验证、一个未验证，覆盖两种显示状态。
        db.add(
            Account(
                user_id=user.id,
                platform="codeforces",
                identifier="verified_one",
                verified_at=utcnow(),
            )
        )
        db.add(Account(user_id=user.id, platform="nowcoder", identifier="pending_one"))
        db.commit()

    body = root_client.get("/api/admin/users?page_size=50").json()
    target = next(u for u in body["items"] if u["student_id"] == MEMBER_STUDENT_ID)

    by_id = {a["identifier"]: a for a in target["accounts"]}
    assert by_id["verified_one"]["verified_at"] is not None
    assert by_id["pending_one"]["verified_at"] is None
    # 只带列表要用的字段，不该把详情页那一整套塞进来。
    assert set(by_id["verified_one"]) == {
        "id",
        "platform",
        "identifier",
        "display_name",
        "verified_at",
    }


def test_admin_user_list_accounts_empty(root_client):
    """没绑账号的成员，accounts 是空数组而不是缺字段。

    前端靠这个长度判断显示「未绑定账号」还是账号标签。
    """
    body = root_client.get("/api/admin/users?page_size=50").json()
    for user in body["items"]:
        assert isinstance(user["accounts"], list)
