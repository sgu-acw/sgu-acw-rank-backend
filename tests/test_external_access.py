"""校外人员注册通道与三级权限边界（未登录 / 校外账号 / 校内成员）测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import db as db_module
from app.config import get_settings
from app.main import create_app
from app.models import (
    Camp,
    DailyProblem,
    DailyProblemSolve,
    ProblemCache,
    ProblemList,
    ProblemListKind,
    Role,
    Signup,
    User,
    utcnow,
)
from app.routers import auth as auth_router

from .conftest import approve_registration

ROOT_STUDENT_ID = "00000000000"
ROOT_PASSWORD = "devpassword123"
MEMBER_STUDENT_ID = "24125023001"
MEMBER_PASSWORD = "memberpass123"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'external.db'}")
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
def db(env):
    with Session(db_module.get_engine()) as session:
        yield session


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


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
                "display_name": "校内测试员",
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


def _seed_visibility_fixtures(db: Session, member: User) -> dict:
    """造出一套跨模块最小数据，用来验证未登录、校外、校内三方可见边界。"""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    db.add(ProblemCache(platform="codeforces", key="1000A", name="Codehorses", rating=900))
    db.add(DailyProblem(day=today, band="800-1000", platform="codeforces", key="1000A"))
    db.add(
        DailyProblemSolve(
            user_id=member.id,
            day=today,
            platform="codeforces",
            key="1000A",
            created_at=utcnow(),
        )
    )

    pl = ProblemList(
        kind=ProblemListKind.LIST,
        title="公开基础题单",
        description="供所有人刷题",
    )
    db.add(pl)

    now = datetime.now(UTC)
    camp = Camp(
        name="校内春季集训",
        description="仅校内成员可见",
        start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=12),
        created_by=member.id,
    )
    db.add(camp)

    signup = Signup(
        name="校内天梯赛选拔",
        description="校内线下赛",
        open_at=now - timedelta(hours=1),
        close_at=now + timedelta(hours=12),
        created_by=member.id,
    )
    db.add(signup)
    db.commit()
    db.refresh(pl)
    db.refresh(camp)
    db.refresh(signup)
    return {
        "list_id": pl.id,
        "camp_id": camp.id,
        "signup_id": signup.id,
        "day": today,
    }


def _register_external(
    client: TestClient,
    *,
    username: str = "guest_alice",
    display_name: str = "外校同学",
    password: str = "ExternalPass123!",
):
    return client.post(
        "/api/auth/register-external",
        json={
            "username": username,
            "display_name": display_name,
            "password": password,
        },
    )


def test_external_registration_login_and_profile_hygiene(app, client, root_client):
    """校外自助注册无需学号与邀请码，自动归一化小写并隐藏校内学籍字段，审核通过后可登录。"""
    resp = _register_external(client, username="Alice_ACM", display_name="小艾")
    assert resp.status_code == 201, resp.text
    me = resp.json()
    assert me["student_id"] == "alice_acm"
    assert me["display_name"] == "小艾"
    assert me["role"] == Role.EXTERNAL
    assert me["enroll_year"] == 0
    assert me["college_name"] is None
    assert me["major_name"] is None
    assert me["class_label"] is None

    # 审核前不发放会话且不可登录
    assert client.get("/api/auth/me").status_code == 401
    pending_login = client.post(
        "/api/auth/login",
        json={"student_id": "ALICE_ACM", "password": "ExternalPass123!"},
    )
    assert pending_login.status_code == 403
    assert "审核" in pending_login.json()["detail"]

    # 管理员审核通过后可登录（大小写不敏感）并管理会话，改名需走资料修改审核
    approve_registration(root_client, "alice_acm")
    login = client.post(
        "/api/auth/login",
        json={"student_id": "ALICE_ACM", "password": "ExternalPass123!"},
    )
    assert login.status_code == 200, login.text
    assert client.get("/api/auth/me").json()["student_id"] == "alice_acm"
    assert client.patch("/api/auth/me", json={"display_name": "小艾改名"}).status_code == 403
    req = client.post(
        "/api/auth/me/change-requests",
        json={"display_name": "小艾改名", "reason": "更正昵称"},
    )
    assert req.status_code == 201, req.text
    assert (
        root_client.post(f"/api/admin/reviews/{req.json()['id']}/approve", json={}).status_code
        == 200
    )
    assert len(client.get("/api/auth/sessions").json()) == 1

    # 登出后用不同大小写用户名重新登录
    assert client.post("/api/auth/logout").status_code == 204
    with TestClient(app) as fresh:
        login = fresh.post(
            "/api/auth/login",
            json={"student_id": "ALICE_ACM", "password": "ExternalPass123!"},
        )
        assert login.status_code == 200, login.text
        assert login.json()["user"]["role"] == Role.EXTERNAL
        assert login.json()["user"]["display_name"] == "小艾改名"


def test_external_registration_validation_duplicate_and_rate_limit(app, client):
    """校验用户名格式（禁止数字/学号开头）、重名 409 与单 IP 频率限制。"""
    for bad in ("ab", "24125011001", "123abc", "_bad", "bad name", "a" * 17):
        resp = _register_external(client, username=bad)
        assert resp.status_code in (400, 422), (
            f"expected rejection for {bad!r}, got {resp.status_code}"
        )

    first = _register_external(client, username="friend01")
    assert first.status_code == 201
    with TestClient(app) as dup_cli:
        dup = _register_external(dup_cli, username="FRIEND01")
        assert dup.status_code == 409

    # 触发按 IP 限流（默认每小时 5 次）
    for i in range(2, auth_router.EXTERNAL_REGISTER_MAX_PER_IP + 1):
        with TestClient(app) as cli:
            r = _register_external(cli, username=f"friend0{i}")
            assert r.status_code == 201
    with TestClient(app) as blocked_cli:
        blocked = _register_external(blocked_cli, username="friend99")
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers


def test_three_tier_visibility_matrix(app, client, member_client, root_client, db):
    """核对未登录 / 校外账号 / 校内成员三级接口可见边界与敏感信息脱敏。"""
    member = db.exec(select(User).where(User.student_id == MEMBER_STUDENT_ID)).one()
    ids = _seed_visibility_fixtures(db, member)
    day = ids["day"]

    with TestClient(app) as ext_client:
        ext_resp = _register_external(ext_client, username="outsider01", display_name="校外访客")
        assert ext_resp.status_code == 201
        ext_id = ext_resp.json()["id"]
        approve_registration(root_client, "outsider01")
        assert (
            ext_client.post(
                "/api/auth/login",
                json={"student_id": "outsider01", "password": "ExternalPass123!"},
            ).status_code
            == 200
        )

        # 1. 公开内容三方都可读
        for c in (client, ext_client, member_client):
            assert c.get("/api/posts").status_code == 200
            assert c.get("/api/honors").status_code == 200
            assert c.get("/api/problem-lists").status_code == 200
            assert c.get(f"/api/problem-lists/{ids['list_id']}").status_code == 200
            assert c.get("/api/learning/problems").status_code == 200
            assert c.get("/api/daily/editorials").status_code == 200

        # 2. 每日一题：未登录与校外看不到校内签到姓名和过题人数；校内成员能看到
        guest_daily = client.get(f"/api/daily?day={day}").json()
        ext_daily = ext_client.get(f"/api/daily?day={day}").json()
        campus_daily = member_client.get(f"/api/daily?day={day}").json()

        assert guest_daily["checked_in_today_names"] == []
        assert ext_daily["checked_in_today_names"] == []
        assert campus_daily["checked_in_today_names"] == [member.display_name]
        assert all(p["members_solved"] == 0 for p in guest_daily["problems"])
        assert all(p["members_solved"] == 0 for p in ext_daily["problems"])

        # 3. 校内专属只读接口：未登录 401，校外 403，校内成员 200
        campus_get_paths = [
            "/api/users",
            f"/api/users/{member.id}",
            "/api/accounts",
            f"/api/accounts/of/{member.id}",
            "/api/practice/rank",
            "/api/practice/rating",
            "/api/practice/dashboard",
            "/api/practice/status",
            "/api/practice/quota",
            "/api/daily/checkin",
            f"/api/daily/solvers?day={day}",
            f"/api/problem-lists/{ids['list_id']}/rank",
            "/api/camps",
            f"/api/camps/{ids['camp_id']}",
            "/api/signups",
            f"/api/signups/{ids['signup_id']}",
            "/api/workshop/override",
            "/api/workshop/schedule",
            f"/api/honors/users/{member.id}",
        ]
        for path in campus_get_paths:
            assert client.get(path).status_code == 401, f"guest should get 401 on {path}"
            assert ext_client.get(path).status_code == 403, f"external should get 403 on {path}"
            assert member_client.get(path).status_code == 200, f"member should get 200 on {path}"

        # 4. 校内专属写接口：校外 403
        assert ext_client.post(f"/api/camps/{ids['camp_id']}/register").status_code == 403
        signup_post = ext_client.post(
            f"/api/signups/{ids['signup_id']}/entries",
            json={"answers": {}},
        )
        assert signup_post.status_code == 403
        assert (
            ext_client.post(
                "/api/accounts", json={"platform": "codeforces", "identifier": "tourist"}
            ).status_code
            == 403
        )
        assert (
            ext_client.post(
                "/api/push/subscribe",
                json={"endpoint": "https://push.example/1", "p256dh": "k", "auth": "a"},
            ).status_code
            == 403
        )
        assert (
            ext_client.post("/api/admin/posts", json={"title": "越权投稿", "body": "x"}).status_code
            == 403
        )

        # 5. 校外账号绝不混入校内成员名录和荣誉个人接口
        roster_ids = {u["id"] for u in member_client.get("/api/users").json()}
        assert ext_id not in roster_ids
        assert member_client.get(f"/api/users/{ext_id}").status_code == 404
        assert member_client.get(f"/api/honors/users/{ext_id}").status_code == 404
        honor_admin_ids = {u["id"] for u in root_client.get("/api/honors/admin/users").json()}
        assert ext_id not in honor_admin_ids


def test_external_user_can_save_learning_runs_and_admin_safeguards(app, root_client, db):
    """校外账号可在题库提交并查询个人记录；管理员侧阻止非法提权/混入集训。"""
    draft = root_client.post(
        "/api/admin/learning/problems",
        json={
            "request_id": str(uuid4()),
            "content": {
                "title": "校外练习题 A+B",
                "chapter": "入门",
                "difficulty": "入门",
                "tags": ["模拟"],
                "statement": "输入两个整数输出和。",
                "environments": ["python"],
                "checker": "tokens",
                "time_ms": 2000,
                "tests": [{"name": "样例 1", "input": "1 2\n", "output": "3\n", "sample": True}],
            },
        },
    ).json()
    pub = root_client.post(
        f"/api/admin/learning/problems/{draft['id']}/publish",
        json={"revision": draft["revision"]},
    ).json()

    with TestClient(app) as ext_client:
        ext = _register_external(ext_client, username="learner01", display_name="外部刷题者").json()
        approve_registration(root_client, "learner01")
        assert (
            ext_client.post(
                "/api/auth/login",
                json={"student_id": "learner01", "password": "ExternalPass123!"},
            ).status_code
            == 200
        )

        run_id = str(uuid4())
        run_resp = ext_client.post(
            "/api/learning/runs",
            json={
                "id": run_id,
                "problem_id": pub["id"],
                "version_id": pub["version_id"],
                "environment": "python",
                "runtime_version": "pyodide-0.27",
                "verdict": "AC",
                "passed": 1,
                "total": 1,
                "time_ms": 12,
                "source": "print(sum(map(int, input().split())))",
            },
        )
        assert run_resp.status_code == 200, run_resp.text
        runs = ext_client.get("/api/learning/runs").json()
        assert len(runs) == 1 and runs[0]["id"] == run_id and runs[0]["verdict"] == "AC"

        # 管理员侧保护：不能发 external 邀请码、不能把校外账号改为 member/admin 或授予 can_post
        bad_invite = root_client.post("/api/admin/invites", json={"grants_role": Role.EXTERNAL})
        assert bad_invite.status_code == 422

        role_patch = root_client.patch(f"/api/admin/users/{ext['id']}", json={"role": Role.MEMBER})
        assert role_patch.status_code == 400
        post_patch = root_client.patch(f"/api/admin/users/{ext['id']}", json={"can_post": True})
        assert post_patch.status_code == 400

        # 管理员可以按角色筛选校外账号、查看统计中的 external_count、停用与删除
        page = root_client.get("/api/admin/users?role=external").json()
        assert page["total"] == 1
        assert page["items"][0]["student_id"] == "learner01"
        stats = root_client.get("/api/admin/stats").json()
        assert stats["external_count"] == 1

        assert root_client.delete(f"/api/admin/users/{ext['id']}").status_code == 204
