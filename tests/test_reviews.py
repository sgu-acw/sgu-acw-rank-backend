"""注册审核、个人资料/改名审核与曾用名留痕测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import db as db_module
from app.config import get_settings
from app.main import create_app
from app.models import (
    AdminNotification,
    ReviewKind,
    ReviewRequest,
    ReviewStatus,
    User,
    UserStatus,
)
from app.reviews import PENDING_REVIEW_FINGERPRINT
from app.routers import auth as auth_router

ROOT_STUDENT_ID = "00000000000"
ROOT_PASSWORD = "devpassword123"
MEMBER_STUDENT_ID = "26125021030"
MEMBER_PASSWORD = "memberpassword123"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'reviews.db'}")
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


def _make_invite(root_client: TestClient, **overrides) -> dict:
    payload = {"max_uses": 1, "ttl_hours": 24, "grants_role": "member", "note": "新生招新"}
    payload.update(overrides)
    resp = root_client.post("/api/admin/invites", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _pending_notification() -> AdminNotification | None:
    with Session(db_module.get_engine()) as db:
        return db.exec(
            select(AdminNotification).where(
                AdminNotification.fingerprint == PENDING_REVIEW_FINGERPRINT
            )
        ).first()


def test_registration_review_approve_lifecycle(app, root_client):
    """校内注册先进入待审核，对外隐藏并触发通知；审核通过后可登录且通知自动解决。"""
    invite = _make_invite(root_client, max_uses=2)

    with TestClient(app) as applicant:
        reg = applicant.post(
            "/api/auth/register",
            json={
                "invite_code": invite["code"],
                "student_id": MEMBER_STUDENT_ID,
                "display_name": "吕奕鹏",
                "password": MEMBER_PASSWORD,
            },
        )
        assert reg.status_code == 201, reg.text
        created = reg.json()
        assert created["status"] == UserStatus.PENDING
        assert not applicant.cookies.get("sgu_acw_session")
        assert applicant.get("/api/auth/me").status_code == 401

        # 审核前登录返回 403 且给出明确提示
        login = applicant.post(
            "/api/auth/login",
            json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
        )
        assert login.status_code == 403
        assert "审核" in login.json()["detail"]

        # 待审核用户不出现在成员名录中
        roster_ids = {u["student_id"] for u in root_client.get("/api/users").json()}
        assert MEMBER_STUDENT_ID not in roster_ids
        roster_with_disabled = {
            u["student_id"] for u in root_client.get("/api/users?include_disabled=true").json()
        }
        assert MEMBER_STUDENT_ID not in roster_with_disabled

        # 统计与聚合通知反映待审核状态
        stats = root_client.get("/api/admin/stats").json()
        assert stats["pending_user_count"] == 1
        assert stats["pending_review_count"] == 1
        notif = _pending_notification()
        assert notif is not None and notif.resolved_at is None
        assert "注册申请 1 条" in notif.body

        # 管理员查询待审队列并通过
        queue = root_client.get("/api/admin/reviews").json()
        assert queue["total"] == 1
        assert queue["pending_registration_count"] == 1
        assert queue["pending_profile_change_count"] == 0
        review = queue["items"][0]
        assert review["kind"] == ReviewKind.REGISTRATION
        assert review["student_id"] == MEMBER_STUDENT_ID
        assert review["invite_code"] == invite["code"]

        approved = root_client.post(
            f"/api/admin/reviews/{review['id']}/approve",
            json={"review_note": "已核对新生名单"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == ReviewStatus.APPROVED
        assert approved.json()["review_note"] == "已核对新生名单"

        # 重复审核返回 409
        again = root_client.post(f"/api/admin/reviews/{review['id']}/approve", json={})
        assert again.status_code == 409

        # 通过后可登录，出现在成员名录，通知自动标已解决
        login_ok = applicant.post(
            "/api/auth/login",
            json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
        )
        assert login_ok.status_code == 200, login_ok.text
        roster_after = {u["student_id"] for u in root_client.get("/api/users").json()}
        assert MEMBER_STUDENT_ID in roster_after

        stats_after = root_client.get("/api/admin/stats").json()
        assert stats_after["pending_user_count"] == 0
        assert stats_after["pending_review_count"] == 0
        notif_after = _pending_notification()
        assert notif_after is not None and notif_after.resolved_at is not None


def test_registration_reject_refunds_invite_and_releases_student_id(app, root_client):
    """驳回注册申请会删除占位账号并退回邀请码次数，允许原学号重新提交注册。"""
    invite = _make_invite(root_client, max_uses=1)

    with TestClient(app) as applicant:
        reg = applicant.post(
            "/api/auth/register",
            json={
                "invite_code": invite["code"],
                "student_id": MEMBER_STUDENT_ID,
                "display_name": "填错的名字",
                "password": MEMBER_PASSWORD,
            },
        )
        assert reg.status_code == 201

        # 邀请码此时已占满
        inv_before = next(
            i for i in root_client.get("/api/admin/invites").json() if i["code"] == invite["code"]
        )
        assert inv_before["use_count"] == 1
        assert inv_before["is_usable"] is False

        review_id = root_client.get("/api/admin/reviews").json()["items"][0]["id"]
        rej = root_client.post(
            f"/api/admin/reviews/{review_id}/reject",
            json={"review_note": "请填写真实姓名"},
        )
        assert rej.status_code == 200, rej.text
        assert rej.json()["status"] == ReviewStatus.REJECTED
        assert rej.json()["user_id"] is None
        assert rej.json()["review_note"] == "请填写真实姓名"

        # 邀请码次数已退回，且同一学号可重新用同一个邀请码注册
        inv_after = next(
            i for i in root_client.get("/api/admin/invites").json() if i["code"] == invite["code"]
        )
        assert inv_after["use_count"] == 0
        assert inv_after["is_usable"] is True

        rereg = applicant.post(
            "/api/auth/register",
            json={
                "invite_code": invite["code"],
                "student_id": MEMBER_STUDENT_ID,
                "display_name": "吕奕鹏",
                "password": MEMBER_PASSWORD,
            },
        )
        assert rereg.status_code == 201, rereg.text

        # 审核历史中保留驳回记录
        rejected_hist = root_client.get("/api/admin/reviews?status=rejected").json()
        assert rejected_hist["total"] == 1
        assert rejected_hist["items"][0]["current_display_name"] == "填错的名字"


def test_external_registration_reject_releases_username(app, root_client):
    """校外注册被驳回后释放用户名，重新注册并审核通过后可登录。"""
    with TestClient(app) as guest:
        reg = guest.post(
            "/api/auth/register-external",
            json={"username": "guest_neo", "display_name": "访客", "password": "ExternalPass123!"},
        )
        assert reg.status_code == 201
        review_id = root_client.get("/api/admin/reviews").json()["items"][0]["id"]
        assert (
            root_client.post(f"/api/admin/reviews/{review_id}/reject", json={}).status_code == 200
        )

        reg2 = guest.post(
            "/api/auth/register-external",
            json={
                "username": "guest_neo",
                "display_name": "访客实名",
                "password": "ExternalPass123!",
            },
        )
        assert reg2.status_code == 201
        review_id2 = root_client.get("/api/admin/reviews").json()["items"][0]["id"]
        assert (
            root_client.post(f"/api/admin/reviews/{review_id2}/approve", json={}).status_code == 200
        )
        login = guest.post(
            "/api/auth/login",
            json={"student_id": "guest_neo", "password": "ExternalPass123!"},
        )
        assert login.status_code == 200


def test_admin_direct_activate_and_delete_sync_reviews(app, root_client):
    """管理员在成员表中直接启用或删除待审账号时，审核单、邀请码和通知保持一致。"""
    invite = _make_invite(root_client, max_uses=2)

    with TestClient(app) as c:
        u1 = c.post(
            "/api/auth/register",
            json={
                "invite_code": invite["code"],
                "student_id": "25125023001",
                "display_name": "直接启用",
                "password": MEMBER_PASSWORD,
            },
        ).json()
        u2 = c.post(
            "/api/auth/register",
            json={
                "invite_code": invite["code"],
                "student_id": "25125023002",
                "display_name": "直接删除",
                "password": MEMBER_PASSWORD,
            },
        ).json()

    # 直接启用 u1
    assert (
        root_client.patch(f"/api/admin/users/{u1['id']}", json={"status": "active"}).status_code
        == 200
    )
    # 直接删除 u2
    assert root_client.delete(f"/api/admin/users/{u2['id']}").status_code == 204

    # 待审核清空，通知已解决，邀请码退回 u2 的 1 次
    assert root_client.get("/api/admin/reviews?status=pending").json()["total"] == 0
    notif = _pending_notification()
    assert notif is not None and notif.resolved_at is not None

    inv = next(
        i for i in root_client.get("/api/admin/invites").json() if i["code"] == invite["code"]
    )
    assert inv["use_count"] == 1

    with Session(db_module.get_engine()) as db:
        rows = list(db.exec(select(ReviewRequest).order_by(ReviewRequest.id)).all())
        assert [r.status for r in rows] == [ReviewStatus.APPROVED, ReviewStatus.REJECTED]


def test_profile_change_review_and_former_name_search(app, root_client):
    """成员改名与更改学籍信息需走审核；通过后自动归档曾用名，管理端按曾用名可搜到。"""
    invite = _make_invite(root_client, max_uses=2)

    with TestClient(app) as member:
        created = member.post(
            "/api/auth/register",
            json={
                "invite_code": invite["code"],
                "student_id": MEMBER_STUDENT_ID,
                "display_name": "吕奕鹏",
                "password": MEMBER_PASSWORD,
            },
        ).json()
        reg_review = root_client.get("/api/admin/reviews").json()["items"][0]
        assert (
            root_client.post(f"/api/admin/reviews/{reg_review['id']}/approve", json={}).status_code
            == 200
        )

        assert (
            member.post(
                "/api/auth/login",
                json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
            ).status_code
            == 200
        )

        # 非管理员无权调用管理员审核接口，也不能直接 PATCH /auth/me 改名
        assert member.get("/api/admin/reviews").status_code == 403
        assert member.patch("/api/auth/me", json={"display_name": "高松灯"}).status_code == 403

        # 登录成员可拉取学院专业选项
        academics = member.get("/api/auth/academics")
        assert academics.status_code == 200
        assert "信息工程学院" in academics.json()["colleges"]

        # 没有实质变更被拒绝
        noop = member.post(
            "/api/auth/me/change-requests",
            json={"display_name": "吕奕鹏", "class_no": 1, "reason": "没改"},
        )
        assert noop.status_code == 400

        # 提交改名 + 改专业 + 改班级申请
        req = member.post(
            "/api/auth/me/change-requests",
            json={
                "display_name": "高松灯",
                "major_override": "计算机科学与技术",
                "class_no": 2,
                "reason": "转专业并更新常用名",
            },
        )
        assert req.status_code == 201, req.text
        req_body = req.json()
        assert req_body["kind"] == ReviewKind.PROFILE_CHANGE
        assert req_body["before"]["display_name"] == "吕奕鹏"
        assert req_body["after"]["display_name"] == "高松灯"
        assert req_body["after"]["major_name"] == "计算机科学与技术"
        assert req_body["after"]["class_no"] == 2

        # 同一时间只能有一条待审核的资料修改申请
        dup = member.post(
            "/api/auth/me/change-requests",
            json={"display_name": "另一个名字"},
        )
        assert dup.status_code == 409

        # 先测撤回
        assert member.delete(f"/api/auth/me/change-requests/{req_body['id']}").status_code == 204
        assert member.delete(f"/api/auth/me/change-requests/{req_body['id']}").status_code == 409
        assert root_client.get("/api/admin/reviews?status=pending").json()["total"] == 0

        # 重新提交并通过
        req2 = member.post(
            "/api/auth/me/change-requests",
            json={
                "display_name": "高松灯",
                "major_override": "计算机科学与技术",
                "class_no": 2,
                "reason": "转专业",
            },
        )
        assert req2.status_code == 201
        req2_id = req2.json()["id"]

        # 按新姓名关键词可以搜到审核单
        searched = root_client.get(
            "/api/admin/reviews",
            params={"status": "pending", "kind": "profile_change", "q": "高松灯"},
        ).json()
        assert searched["total"] == 1 and searched["items"][0]["id"] == req2_id

        approved = root_client.post(
            f"/api/admin/reviews/{req2_id}/approve",
            json={"review_note": "同意变更"},
        )
        assert approved.status_code == 200, approved.text

        # 生效检查：当前资料已变更，旧姓名已自动沉淀到 former_names
        me_after = member.get("/api/auth/me").json()
        assert me_after["display_name"] == "高松灯"
        assert me_after["major_name"] == "计算机科学与技术"
        assert me_after["class_no"] == 2

        detail = root_client.get(f"/api/admin/members/{created['id']}").json()
        assert detail["former_names"] == ["吕奕鹏"]

        # 管理员成员搜索用曾用名「吕奕鹏」依然能查到「高松灯」
        found = root_client.get("/api/admin/users", params={"q": "吕奕鹏"}).json()
        assert found["total"] == 1
        assert found["items"][0]["student_id"] == MEMBER_STUDENT_ID
        assert found["items"][0]["display_name"] == "高松灯"
        assert found["items"][0]["former_names"] == ["吕奕鹏"]

        # 我的申请历史可见已通过记录及审批人信息
        mine = member.get("/api/auth/me/change-requests").json()
        assert len(mine) == 2
        assert mine[0]["status"] == ReviewStatus.APPROVED
        assert mine[0]["review_note"] == "同意变更"
        assert mine[1]["status"] == ReviewStatus.CANCELLED


def test_profile_change_reject_and_external_restrictions_and_admin_rename_archive(app, root_client):
    """驳回资料修改不改动原资料；校外仅限改昵称；管理员直接改名同样记录曾用名。"""
    with TestClient(app) as ext:
        reg = ext.post(
            "/api/auth/register-external",
            json={
                "username": "ext_user",
                "display_name": "初始外校名",
                "password": "ExternalPass123!",
            },
        )
        assert reg.status_code == 201
        ext_id = reg.json()["id"]
        reg_review = root_client.get("/api/admin/reviews").json()["items"][0]
        assert (
            root_client.post(f"/api/admin/reviews/{reg_review['id']}/approve", json={}).status_code
            == 200
        )
        assert (
            ext.post(
                "/api/auth/login",
                json={"student_id": "ext_user", "password": "ExternalPass123!"},
            ).status_code
            == 200
        )

        # 校外不能提交学院/专业/班级修改
        bad_academic = ext.post(
            "/api/auth/me/change-requests",
            json={"college_override": "信息工程学院"},
        )
        assert bad_academic.status_code == 400
        assert "校外" in bad_academic.json()["detail"]

        # 提交改昵称并被驳回 -> 原昵称不变
        req = ext.post(
            "/api/auth/me/change-requests",
            json={"display_name": "不合规昵称", "reason": "想换个名字"},
        )
        assert req.status_code == 201
        rej = root_client.post(
            f"/api/admin/reviews/{req.json()['id']}/reject",
            json={"review_note": "请使用常用昵称"},
        )
        assert rej.status_code == 200
        assert ext.get("/api/auth/me").json()["display_name"] == "初始外校名"

    # 管理员直接改名也会归档 former_names 且递增 honor_revision
    with Session(db_module.get_engine()) as db:
        before_rev = db.get(User, ext_id).honor_revision

    patched = root_client.patch(
        f"/api/admin/users/{ext_id}",
        json={"display_name": "规范昵称"},
    )
    assert patched.status_code == 200
    assert patched.json()["former_names"] == ["初始外校名"]

    with Session(db_module.get_engine()) as db:
        after_rev = db.get(User, ext_id).honor_revision
        assert after_rev == before_rev + 1

    root_before = root_client.get("/api/auth/me").json()
    pending = root_client.post("/api/auth/me/change-requests", json={"display_name": "站长新名字"})
    assert pending.status_code == 201, pending.text
    assert root_client.get("/api/auth/me").json()["display_name"] == root_before["display_name"]
    review_id = pending.json()["id"]
    approval_path = f"/api/admin/reviews/{review_id}/approve"
    assert root_client.post(approval_path, json={}).status_code == 403
    assert root_client.post(f"/api/admin/reviews/{review_id}/reject", json={}).status_code == 403

    created_admin = root_client.post(
        "/api/admin/users",
        json={
            "student_id": MEMBER_STUDENT_ID,
            "password": MEMBER_PASSWORD,
            "display_name": "另一位管理员",
            "role": "admin",
        },
    )
    assert created_admin.status_code == 201, created_admin.text
    with TestClient(app) as approver:
        login = approver.post(
            "/api/auth/login", json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD}
        )
        assert login.status_code == 200, login.text
        assert approver.post(approval_path, json={}).status_code == 200

    assert root_client.get("/api/auth/me").json()["display_name"] == "站长新名字"
    root_row = root_client.get(f"/api/admin/users/{root_before['id']}").json()
    assert root_before["display_name"] in root_row["former_names"]
