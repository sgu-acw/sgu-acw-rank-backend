"""重构回归、安全、并发与新增功能测试。"""

from __future__ import annotations

import io
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import openpyxl
import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_engine, init_db, reset_engine
from app.learning_models import LearningProblem, LearningRun, LearningVersion
from app.main import create_app
from app.models import (
    Account,
    Camp,
    DailyProblem,
    DailyProblemSolve,
    DailyProgress,
    Editorial,
    EditorialStatus,
    Invite,
    Platform,
    Post,
    ProblemCache,
    ProblemList,
    ProblemListItem,
    RefreshQuota,
    Role,
    Signup,
    SolvedSnapshot,
    User,
    UserSession,
    UserStatus,
    WorkshopStatusOverride,
    utcnow,
)
from app.platforms import Profile
from app.routers.auth import reset_login_rate_limits
from app.security import hash_password
from app.sync import current_practice_day


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "refactor.db"
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("SGU_ACW_SECRET_KEY", "test-secret-refactor-key")
    monkeypatch.setenv("SGU_ACW_COOKIE_SECURE", "false")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_LEARNING_ENABLED", "true")
    monkeypatch.setenv("SGU_ACW_VERIFY_COOLDOWN_SECONDS", "60")
    reset_login_rate_limits()
    get_settings.cache_clear()
    reset_engine()
    init_db()
    app = create_app()
    yield app
    reset_login_rate_limits()
    reset_engine()
    get_settings.cache_clear()


def _create_user(
    db: Session,
    *,
    student_id: str,
    display_name: str,
    password: str = "pass1234",
    role: str = Role.MEMBER,
    status: str = UserStatus.ACTIVE,
    can_post: bool = False,
) -> User:
    user = User(
        student_id=student_id,
        display_name=display_name,
        password_hash=hash_password(password),
        role=role,
        status=status,
        can_post=can_post,
        enroll_year=2024,
        level=2,
        college_code="01",
        major_code="01",
        class_no=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login_client(app, student_id: str, password: str = "pass1234", headers: dict | None = None):
    c = TestClient(app, headers=headers or {})
    r = c.post("/api/auth/login", json={"student_id": student_id, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_origin_secret_enforced_across_all_api_routers(tmp_path, monkeypatch):
    """所有经 nginx 暴露的 /api/* 路由（除 /api/healthz）都必须校验 X-Origin-Secret。"""
    db_file = tmp_path / "origin.db"
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("SGU_ACW_SECRET_KEY", "test-origin-key")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "top-secret-edge-token")
    get_settings.cache_clear()
    reset_engine()
    init_db()
    app = create_app()

    try:
        raw = TestClient(app)
        assert raw.get("/api/healthz").status_code == 200

        protected_gets = [
            "/api/daily",
            "/api/daily/editorials",
            "/api/camps",
            "/api/posts",
            "/api/signups",
            "/api/workshop/override",
            "/api/push/vapid-key",
            "/api/admin/stats",
            "/api/admin/notifications",
            "/api/learning/problems",
        ]
        for path in protected_gets:
            resp = raw.get(path)
            assert resp.status_code == 403, f"{path} should reject missing origin secret"

        # 携带正确来源密钥后，公开接口放行、鉴权接口返回 401（非 403 origin 错误）
        authed_edge = TestClient(app, headers={"X-Origin-Secret": "top-secret-edge-token"})
        assert authed_edge.get("/api/posts").status_code == 200
        assert authed_edge.get("/api/daily").status_code == 200
        assert authed_edge.get("/api/workshop/override").status_code == 401
        assert authed_edge.get("/api/camps").status_code == 401
        assert authed_edge.get("/api/signups").status_code == 401
        assert authed_edge.get("/api/admin/stats").status_code == 401
        assert authed_edge.get("/api/admin/notifications").status_code == 401
    finally:
        reset_engine()
        get_settings.cache_clear()


def test_session_last_seen_throttled_and_session_management(env):
    """会话 last_seen_at 5 分钟内不重复写入；支持查看和踢出其他设备。"""
    with Session(get_engine()) as db:
        user = _create_user(db, student_id="2401010101", display_name="张三")
        uid = user.id

    c1 = _login_client(env, "2401010101", headers={"User-Agent": "Chrome/120.0 Macintosh"})
    c2 = _login_client(env, "2401010101", headers={"User-Agent": "Safari/17.0 iPhone"})

    with Session(get_engine()) as db:
        sessions = db.exec(select(UserSession).where(UserSession.user_id == uid)).all()
        assert len(sessions) == 2
        before = {s.id: s.last_seen_at for s in sessions}

    # 立刻连续请求，不应反复更新 last_seen_at
    assert c1.get("/api/auth/me").status_code == 200
    assert c1.get("/api/auth/me").status_code == 200

    with Session(get_engine()) as db:
        sessions = db.exec(select(UserSession).where(UserSession.user_id == uid)).all()
        for s in sessions:
            assert s.last_seen_at == before[s.id]

    # 查看会话列表，当前会话置顶并标记 is_current
    listed = c1.get("/api/auth/sessions")
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 2
    assert items[0]["is_current"] is True
    assert items[1]["is_current"] is False
    assert "Chrome" in items[0]["device_label"]
    assert "iPhone" in items[1]["device_label"]

    # 不能通过踢出接口删当前会话
    assert c1.delete(f"/api/auth/sessions/{items[0]['id']}").status_code == 400
    # 可以踢出其他设备
    assert c1.delete(f"/api/auth/sessions/{items[1]['id']}").status_code == 204
    assert c2.get("/api/auth/me").status_code == 401
    assert c1.get("/api/auth/me").status_code == 200

    # 再登一个设备后，一键下线其他设备
    c3 = _login_client(env, "2401010101", headers={"User-Agent": "Firefox/125.0 Windows"})
    revoked = c1.post("/api/auth/sessions/revoke-others")
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] == 1
    assert c3.get("/api/auth/me").status_code == 401
    assert c1.get("/api/auth/me").status_code == 200


def test_login_rate_limit_and_totp_whitespace(env):
    """登录连续失败达到阈值触发 429；TOTP 支持带空格输入。"""
    with Session(get_engine()) as db:
        _create_user(db, student_id="2401010102", display_name="李四")

    c = TestClient(env)
    for _ in range(10):
        resp = c.post("/api/auth/login", json={"student_id": "2401010102", "password": "wrong"})
        assert resp.status_code == 401

    blocked = c.post("/api/auth/login", json={"student_id": "2401010102", "password": "wrong"})
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers

    # 清除限流后测试 TOTP 含空格码
    reset_login_rate_limits()
    authed = _login_client(env, "2401010102")
    begin = authed.post("/api/auth/me/totp/begin").json()
    code = pyotp.TOTP(begin["secret"]).now()
    spaced = f"{code[:3]} {code[3:]}"
    assert authed.post("/api/auth/me/totp/enable", json={"code": spaced}).status_code == 200

    # 用新客户端测试带空格 TOTP 登录
    c2 = TestClient(env)
    step1 = c2.post("/api/auth/login", json={"student_id": "2401010102", "password": "pass1234"})
    assert step1.status_code == 200
    assert step1.json()["totp_required"] is True
    step2 = c2.post(
        "/api/auth/login/totp", json={"ticket": step1.json()["ticket"], "code": spaced}
    )
    assert step2.status_code == 200
    assert step2.json()["totp_required"] is False


def test_delete_user_and_account_clean_all_foreign_keys(env):
    """删除创建过文章/集训/报名/状态/邀请码且有每日过题与评测记录的成员不触发外键错误。"""
    with Session(get_engine()) as db:
        root = _create_user(db, student_id="2401010001", display_name="Root", role=Role.ROOT)
        target = _create_user(db, student_id="2401010002", display_name="前管理员", role=Role.ADMIN)

        acc = Account(
            user_id=target.id,
            platform=Platform.CODEFORCES,
            identifier="old_admin_cf",
            verified_at=utcnow(),
        )
        db.add(acc)
        db.flush()

        dp = DailyProblem(day="2026-03-01", band="800-1000", platform="codeforces", key="1A")
        db.add(dp)
        db.flush()
        db.add(
            DailyProblemSolve(
                day="2026-03-01",
                platform="codeforces",
                key="1A",
                user_id=target.id,
                account_id=acc.id,
            )
        )

        lp = LearningProblem(
            creation_id=str(uuid.uuid4()),
            creation_hash="h",
            draft_json="{}",
            published_revision=1,
        )
        db.add(lp)
        db.flush()
        lv = LearningVersion(id=str(uuid.uuid4()), problem_id=lp.id, revision=1, content_json="{}")
        db.add(lv)
        db.flush()
        db.add(
            LearningRun(
                id=str(uuid.uuid4()),
                user_id=target.id,
                version_id=lv.id,
                environment="python",
                runtime_version="3.12",
                source="print(1)",
                verdict="AC",
                request_hash="rh",
            )
        )

        db.add(
            Invite(
                code="INV-TEST-1",
                max_uses=5,
                created_by=target.id,
                expires_at=utcnow() + timedelta(days=7),
            )
        )
        db.add(ProblemList(title="旧题单", created_by=target.id))
        db.add(Post(slug="legacy-post", title="历史文章", body="hello", author_id=target.id))
        db.add(
            Camp(
                name="历史集训",
                start_at=utcnow(),
                end_at=utcnow() + timedelta(days=1),
                created_by=target.id,
            )
        )
        db.add(Signup(name="历史报名", open_at=utcnow(), created_by=target.id))
        db.add(WorkshopStatusOverride(label="测试中", updated_by=target.id))
        db.commit()
        target_id = target.id
        root_id = root.id

    root_client = _login_client(env, "2401010001")
    # 管理员不能停用或降级自己
    r_self = root_client.patch(f"/api/admin/users/{root_id}", json={"status": "disabled"})
    assert r_self.status_code == 403

    r = root_client.delete(f"/api/admin/users/{target_id}")
    assert r.status_code == 204, r.text

    with Session(get_engine()) as db:
        assert db.get(User, target_id) is None
        # 历史内容保留并转移归属到 root
        post = db.exec(select(Post).where(Post.slug == "legacy-post")).one()
        assert post.author_id == root_id
        camp = db.exec(select(Camp).where(Camp.name == "历史集训")).one()
        assert camp.created_by == root_id
        signup = db.exec(select(Signup).where(Signup.name == "历史报名")).one()
        assert signup.created_by == root_id
        inv = db.exec(select(Invite).where(Invite.code == "INV-TEST-1")).one()
        assert inv.created_by is None
        assert db.exec(select(LearningRun).where(LearningRun.user_id == target_id)).all() == []
        assert (
            db.exec(select(DailyProblemSolve).where(DailyProblemSolve.user_id == target_id)).all()
            == []
        )


def test_admin_cannot_self_disable_or_demote(env):
    """普通管理员不能停用或撤销自己的管理员权限。"""
    with Session(get_engine()) as db:
        admin = _create_user(db, student_id="2401010011", display_name="管一", role=Role.ADMIN)
        aid = admin.id

    c = _login_client(env, "2401010011")
    assert c.patch(f"/api/admin/users/{aid}", json={"role": Role.MEMBER}).status_code == 400
    assert (
        c.patch(f"/api/admin/users/{aid}", json={"status": UserStatus.DISABLED}).status_code == 400
    )


def test_expired_unverified_account_can_be_reclaimed_and_verify_cooldown_is_atomic(env):
    """过期未验证的占位账号允许被他人认领；并发 verify_account 受写锁串行化保护。"""
    with Session(get_engine()) as db:
        u1 = _create_user(db, student_id="2401010201", display_name="甲")
        _create_user(db, student_id="2401010202", display_name="乙")
        stale = Account(
            user_id=u1.id,
            platform=Platform.CODEFORCES,
            identifier="tourist_clone",
            verify_token="ACW-EXPIRED",
            verify_expires_at=utcnow() - timedelta(hours=1),
        )
        db.add(stale)
        db.commit()

    c2 = _login_client(env, "2401010202")
    with patch(
        "app.routers.accounts.fetch_profile",
        return_value=Profile(
            platform=Platform.CODEFORCES, identifier="tourist_clone", signature=""
        ),
    ):
        created = c2.post(
            "/api/accounts", json={"platform": Platform.CODEFORCES, "identifier": "tourist_clone"}
        )
    assert created.status_code == 201, created.text
    acc_id = created.json()["id"]

    # 并发触发两次 verify_account：只允许一次进入外部 check_signature，另一请求 429。
    calls = 0
    calls_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def fake_check(*_args, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        return False, "签名中未找到验证码"

    results: list[int] = []

    def worker():
        client = _login_client(env, "2401010202")
        barrier.wait()
        resp = client.post(f"/api/accounts/{acc_id}/verify")
        results.append(resp.status_code)

    with patch("app.routers.accounts.check_signature", side_effect=fake_check):
        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    assert sorted(results) == [200, 429]
    assert calls == 1


def test_practice_invalid_platform_400_and_refresh_guards_and_dashboard_streak(env):
    """非法平台 400、空账号刷新不扣额度、并发刷新原子扣额度以及连续刷题天数。"""
    with Session(get_engine()) as db:
        u = _create_user(db, student_id="2401010301", display_name="丙")
        uid = u.id

    c = _login_client(env, "2401010301")
    assert c.get("/api/practice/rank?platform=unknown_oj").status_code == 400
    assert c.get("/api/practice/rating?platform=unknown_oj").status_code == 400

    # 无已验证账号时返回 400 且不消耗额度
    assert c.post("/api/practice/refresh").status_code == 400
    assert c.get("/api/practice/quota").json()["remaining"] == RefreshQuota.WEEKLY_LIMIT

    today = current_practice_day()
    yesterday = (datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    with Session(get_engine()) as db:
        verified = Account(
            user_id=uid,
            platform=Platform.CODEFORCES,
            identifier="cf_ok",
            verified_at=utcnow(),
            sync_flag="privacy",
            consecutive_failures=4,
            last_error="private profile",
        )
        unverified = Account(
            user_id=uid,
            platform=Platform.ATCODER,
            identifier="ac_pending",
            verify_token="ACW-1234",
            verify_expires_at=utcnow() + timedelta(hours=12),
        )
        db.add(verified)
        db.add(unverified)
        db.flush()
        db.add(
            DailyProgress(
                account_id=verified.id, platform=Platform.CODEFORCES, date=today, solved_count=2
            )
        )
        db.add(
            DailyProgress(
                account_id=verified.id,
                platform=Platform.CODEFORCES,
                date=yesterday,
                solved_count=1,
            )
        )
        # 预设已用 6 次（只剩最后 1 次额度）
        from app.routers.practice import _current_week_start

        db.add(RefreshQuota(user_id=uid, week_start=_current_week_start(), used=6))
        db.commit()

    dash = c.get("/api/practice/dashboard")
    assert dash.status_code == 200
    body = dash.json()
    assert body["practice_streak"] == 2
    assert len(body["accounts"]) == 2
    by_plat = {a["platform"]: a for a in body["accounts"]}
    assert by_plat[Platform.ATCODER]["verified"] is False
    assert by_plat[Platform.CODEFORCES]["sync_flag"] == "privacy"
    assert by_plat[Platform.CODEFORCES]["consecutive_failures"] == 4

    # 并发抢最后 1 次手动刷新额度：只能 1 个 200、1 个 429
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def do_refresh():
        cli = _login_client(env, "2401010301")
        barrier.wait()
        r = cli.post("/api/practice/refresh")
        statuses.append(r.status_code)

    with (
        patch("app.routers.practice.sync_account", return_value=(True, "ok", 0)),
        patch("app.routers.practice.sync_rating", return_value=(True, "ok")),
    ):
        t1 = threading.Thread(target=do_refresh)
        t2 = threading.Thread(target=do_refresh)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    assert sorted(statuses) == [200, 429]


def test_problemlists_fixes_and_batch_import(env):
    """题单：learn 先校验再写库、合集去重计数、严格排序校验与批量加题。"""
    with Session(get_engine()) as db:
        _create_user(db, student_id="2401010401", display_name="管", role=Role.ADMIN)
        m = _create_user(db, student_id="2401010402", display_name="队员")
        acc = Account(
            user_id=m.id, platform=Platform.CODEFORCES, identifier="m_cf", verified_at=utcnow()
        )
        db.add(acc)
        db.flush()
        db.add(
            SolvedSnapshot(
                account_id=acc.id,
                platform=Platform.CODEFORCES,
                problems_json=json.dumps(["1A", "2B"]),
            )
        )
        db.commit()

    admin = _login_client(env, "2401010401")
    col = admin.post("/api/problem-lists", json={"kind": "collection", "title": "合集"}).json()
    l1 = admin.post(
        "/api/problem-lists",
        json={"kind": "list", "title": "子单1", "collection_id": col["id"]},
    ).json()
    l2 = admin.post(
        "/api/problem-lists",
        json={"kind": "list", "title": "子单2", "collection_id": col["id"]},
    ).json()

    # 添加不存在的 learn 题必须报错且不能产生脏行
    bad_learn = admin.post(
        f"/api/problem-lists/{l1['id']}/problems",
        json={"platform": Platform.LEARN, "key": "99999"},
    )
    assert bad_learn.status_code == 400
    with Session(get_engine()) as db:
        assert (
            db.exec(
                select(ProblemListItem).where(ProblemListItem.list_id == l1["id"])
            ).all()
            == []
        )

    # 批量加题（含重复与合法项），两个子单包含重叠题 1A
    from app.problems import ProblemInfo

    def fake_fetch(_db, platform, key):
        return ProblemInfo(platform=platform, key=key, name=f"Title {key}", rating=1200)

    with patch("app.routers.problemlists._fetch_and_cache", side_effect=fake_fetch):
        batch1 = admin.post(
            f"/api/problem-lists/{l1['id']}/problems/batch",
            json={
                "items": [
                    {"platform": "codeforces", "key": "1A"},
                    {"platform": "codeforces", "key": "1A"},
                    {"platform": "codeforces", "key": "2B"},
                ]
            },
        )
        assert batch1.status_code == 200
        assert len(batch1.json()["added"]) == 2
        assert len(batch1.json()["skipped"]) == 1

        batch2 = admin.post(
            f"/api/problem-lists/{l2['id']}/problems/batch",
            json={"items": [{"platform": "codeforces", "key": "1A"}]},
        )
        assert batch2.status_code == 200
        assert len(batch2.json()["added"]) == 1

    # 合集去重：共 2 道独立题，队员做完 2 道应为 100%
    rank = admin.get(f"/api/problem-lists/{col['id']}/rank").json()
    assert rank["total"] == 2
    assert rank["entries"][0]["done_count"] == 2
    assert rank["entries"][0]["percent"] == 100

    # reorder 严格校验
    assert admin.post("/api/problem-lists/reorder", json={"ids": [999999]}).status_code == 404
    assert (
        admin.post("/api/problem-lists/reorder", json={"ids": [l1["id"], l1["id"]]}).status_code
        == 400
    )

    # 删合集先 flush 不触发外键错误
    assert admin.delete(f"/api/problem-lists/{col['id']}").status_code == 204


def test_daily_editorials_posts_signups_push_workshop_learning(env):
    """覆盖每日一题、题解发布校验、弹窗规则、Excel 安全导出、推送安全、状态并发、学习题下架删除。"""
    with Session(get_engine()) as db:
        admin_u = _create_user(db, student_id="2401010501", display_name="管理", role=Role.ADMIN)
        disabled_admin = _create_user(
            db,
            student_id="2401010502",
            display_name="停用管理",
            role=Role.ADMIN,
            status=UserStatus.DISABLED,
        )
        member = _create_user(db, student_id="2401010503", display_name="同天两题王")
        dp1 = DailyProblem(day="2026-04-01", band="800-1000", platform="codeforces", key="100A")
        dp2 = DailyProblem(day="2026-04-01", band="1100-1300", platform="codeforces", key="100B")
        db.add(dp1)
        db.add(dp2)
        db.flush()
        db.add(ProblemCache(platform="codeforces", key="100A", name="Special DP Problem"))
        db.add(
            DailyProblemSolve(
                day="2026-04-01", platform="codeforces", key="100A", user_id=member.id
            )
        )
        db.add(
            DailyProblemSolve(
                day="2026-04-01", platform="codeforces", key="100B", user_id=member.id
            )
        )
        ed_empty = Editorial(
            daily_problem_id=dp1.id,
            day="2026-04-01",
            platform="codeforces",
            key="100A",
            status=EditorialStatus.FAILED,
            body="",
        )
        db.add(ed_empty)
        db.commit()
        ed_id = ed_empty.id
        admin_id = admin_u.id
        disabled_admin_id = disabled_admin.id

    admin = _login_client(env, "2401010501")
    member_c = _login_client(env, "2401010503")

    # 每日一题：非法日期 400；签到人名去重
    assert admin.get("/api/daily/solvers?day=not-a-date").status_code == 400
    daily_board = admin.get("/api/daily?day=2026-04-01").json()
    assert daily_board["checked_in_today_names"] == ["同天两题王"]

    # 题解：空正文/FAILED 不能公布；支持按题名搜索
    assert admin.post(f"/api/admin/editorials/{ed_id}/publish").status_code == 400
    assert admin.post(f"/api/daily/editorials/{ed_id}/publish").status_code == 400
    searched = admin.get("/api/admin/editorials?q=Special DP").json()
    assert searched["total"] == 1

    # 文章弹窗校验：未上架文章不可弹窗；过期截止时间拒绝
    post = admin.post("/api/admin/posts", json={"title": "草稿公告", "body": "内容"}).json()
    assert (
        admin.post(f"/api/admin/posts/{post['id']}/publish", json={"published": False}).status_code
        == 200
    )
    assert (
        admin.post(f"/api/admin/posts/{post['id']}/popup", json={"on": True}).status_code == 400
    )
    assert (
        admin.post(f"/api/admin/posts/{post['id']}/publish", json={"published": True}).status_code
        == 200
    )
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert (
        admin.post(
            f"/api/admin/posts/{post['id']}/popup", json={"on": True, "until": past}
        ).status_code
        == 422
    )

    # 报名 Excel 导出：非法工作表名字符 + 公式注入防护
    signup_resp = admin.post(
        "/api/admin/signups",
        json={
            "name": "2026/05:招新[面试]*?",
            "open_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "fields": [{"label": "备注", "type": "text", "required": False}],
        },
    )
    assert signup_resp.status_code == 201, signup_resp.text
    signup = signup_resp.json()
    field_key = signup["fields"][0]["key"]
    assert (
        member_c.post(
            f"/api/signups/{signup['id']}/entries",
            json={"answers": {field_key: "=CMD|' /C calc'!A0"}},
        ).status_code
        == 200
    )
    exported = admin.get(f"/api/admin/signups/{signup['id']}/export")
    assert exported.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(exported.content))
    ws = wb.active
    assert ws.title
    rows = list(ws.iter_rows(values_only=True))
    assert rows[1][3] == "'=CMD|' /C calc'!A0"

    # 推送：外链 URL 被拒绝；admins 群体排除已停用管理员
    bad_push = admin.post(
        "/api/admin/push/announce",
        json={
            "request_id": "req-1234567890abcdef",
            "title": "通知",
            "body": "hi",
            "url": "https://evil.example/phish",
            "audience": "admins",
        },
    )
    assert bad_push.status_code == 422
    from app.routers.push import _audience_user_ids

    with Session(get_engine()) as db:
        admin_targets = _audience_user_ids(db, "admins")
        assert admin_id in admin_targets
        assert disabled_admin_id not in admin_targets

    # Workshop 并发写入覆盖状态：只允许 1 条处于 active 且在窗口内
    barrier = threading.Barrier(2)

    def put_override(label: str):
        cli = _login_client(env, "2401010501")
        barrier.wait()
        resp = cli.put("/api/admin/workshop/override", json={"label": label})
        assert resp.status_code == 201

    t1 = threading.Thread(target=put_override, args=("比赛占用A",))
    t2 = threading.Thread(target=put_override, args=("比赛占用B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    with Session(get_engine()) as db:
        active_rows = db.exec(
            select(WorkshopStatusOverride).where(WorkshopStatusOverride.active.is_(True))
        ).all()
        assert len(active_rows) == 1

    # 学习题库：发布 -> 提交 -> 查询单条记录 -> 下架 -> 删除
    created_lp = admin.post(
        "/api/admin/learning/problems",
        json={
            "request_id": str(uuid.uuid4()),
            "content": {
                "title": "A+B 测试",
                "chapter": "入门",
                "difficulty": "入门",
                "tags": ["语法"],
                "statement": "输出 a+b",
                "environments": ["python"],
                "checker": "tokens",
                "time_ms": 1000,
                "tests": [{"name": "s1", "input": "1 2\n", "output": "3\n", "sample": True}],
            },
        },
    ).json()
    pid = created_lp["id"]
    published = admin.post(
        f"/api/admin/learning/problems/{pid}/publish", json={"revision": created_lp["revision"]}
    ).json()
    run_id = str(uuid.uuid4())
    assert (
        member_c.post(
            "/api/learning/runs",
            json={
                "id": run_id,
                "version_id": published["version_id"],
                "environment": "python",
                "runtime_version": "py-3.12",
                "source": "print(sum(map(int, input().split())))",
                "verdict": "AC",
            },
        ).status_code
        == 200
    )
    filtered_runs = member_c.get(f"/api/learning/runs?problem_id={pid}").json()
    assert len(filtered_runs) == 1
    detail_run = member_c.get(f"/api/learning/runs/{run_id}").json()
    assert detail_run["code"] == "print(sum(map(int, input().split())))"

    # 已发布不能直接删
    assert admin.delete(f"/api/admin/learning/problems/{pid}").status_code == 409
    # 先下架再删除成功
    unpub = admin.post(f"/api/admin/learning/problems/{pid}/unpublish")
    assert unpub.status_code == 200
    assert unpub.json()["published_revision"] is None
    assert admin.delete(f"/api/admin/learning/problems/{pid}").status_code == 204
    assert admin.get(f"/api/learning/problems/{pid}").status_code == 404
