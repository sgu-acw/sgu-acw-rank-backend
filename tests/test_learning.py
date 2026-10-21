from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select


@pytest.fixture()
def local_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SGU_LEARN_LOCAL", "1")
    monkeypatch.setenv("SGU_LEARN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path}/learning.db")
    monkeypatch.setenv("SGU_ACW_COOKIE_SECURE", "false")
    monkeypatch.setenv("SGU_ACW_COOKIE_DOMAIN", "")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", "LocalLearning123!")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", "00000000000")
    from app.config import get_settings
    from app.db import reset_engine

    get_settings.cache_clear()
    reset_engine()
    from app.learning_dev import create_learning_app

    with TestClient(create_learning_app()) as c:
        yield c
    reset_engine()
    get_settings.cache_clear()


def login(client):
    assert (
        client.post(
            "/api/auth/login", json={"student_id": "00000000000", "password": "LocalLearning123!"}
        ).status_code
        == 200
    )


def test_local_guard_precedes_database_access(tmp_path, monkeypatch):
    monkeypatch.delenv("SGU_LEARN_LOCAL", raising=False)
    from app.learning_dev import assert_local_database

    with pytest.raises(RuntimeError, match="requires"):
        assert_local_database()
    assert not (tmp_path / "learning.db").exists()


def test_production_app_registers_learning_routes():
    """题库是正式功能，路由注册在生产应用上；能否写由开关和权限决定。"""
    from app.main import create_app

    assert any("/learning/" in r.path for r in create_app().routes)


def test_published_versions_and_optimistic_drafts(local_client):
    c = local_client
    original = c.get("/api/learning/problems/1").json()
    assert original["content"]["title"] == "A + B"
    assert c.get("/api/admin/learning/problems").status_code == 401
    login(c)
    draft = c.get("/api/admin/learning/problems").json()[0]
    changed = {**draft["content"], "title": "新标题"}
    endpoint = "/api/admin/learning/problems/1"
    saved = c.put(endpoint, json={"revision": 1, "content": changed}).json()
    assert saved["revision"] == 2
    assert c.put(endpoint, json={"revision": 1, "content": changed}).json()["revision"] == 2
    assert c.put(endpoint, json={"revision": 1, "content": draft["content"]}).status_code == 409
    assert c.get("/api/learning/problems/1").json() == original
    assert c.post(endpoint + "/publish", json={"revision": 1}).status_code == 409
    published = c.post(endpoint + "/publish", json={"revision": 2}).json()
    assert c.post(endpoint + "/publish", json={"revision": 2}).json() == published
    assert published["content"]["title"] == "新标题"
    assert c.get("/api/learning/versions/" + original["version_id"]).json() == original
    invalid = {**changed, "tests": []}
    assert c.put(endpoint, json={"revision": 2, "content": invalid}).status_code == 422


def test_create_retries_and_changed_content_conflict(local_client):
    c = local_client
    login(c)
    content = c.get("/api/learning/problems/1").json()["content"]
    request = {"request_id": str(uuid4()), "content": content}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda _: c.post("/api/admin/learning/problems", json=request), range(2))
        )
    assert [r.status_code for r in responses] == [201, 201]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert len(c.get("/api/admin/learning/problems").json()) == 9
    changed = {**request, "content": {**content, "title": "different"}}
    assert c.post("/api/admin/learning/problems", json=changed).status_code == 409


def test_concurrent_draft_writers_cannot_overwrite(local_client):
    c = local_client
    login(c)
    content = c.get("/api/learning/problems/1").json()["content"]
    barrier = Barrier(2)

    def write(title):
        barrier.wait()
        return c.put(
            "/api/admin/learning/problems/1",
            json={"revision": 1, "content": {**content, "title": title}},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(write, ["first", "second"]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    accepted = next(r.json()["content"]["title"] for r in responses if r.status_code == 200)
    actual = c.get("/api/admin/learning/problems").json()[0]
    assert actual["content"]["title"] == accepted
    assert actual["revision"] == 2


def test_self_reported_run_is_idempotent_and_user_cascade(local_client):
    c = local_client
    problem = c.get("/api/learning/problems/1").json()
    payload = {
        "id": str(uuid4()),
        "version_id": problem["version_id"],
        "environment": "java8",
        "runtime_version": "test-only",
        "source": "class Main {}",
        "verdict": "AC",
    }
    assert c.post("/api/learning/runs", json=payload).status_code == 401
    login(c)
    assert c.post("/api/learning/runs", json=payload).json()["source"] == "client"
    assert c.post("/api/learning/runs", json=payload).status_code == 200
    assert len(c.get("/api/learning/runs").json()) == 1
    assert c.post("/api/learning/runs", json={**payload, "source": "changed"}).status_code == 409
    from app.db import get_engine
    from app.learning_models import LearningRun
    from app.models import User, UserSession

    with Session(get_engine()) as db:
        user = db.exec(select(User).where(User.student_id == "00000000000")).one()
        for session in db.exec(select(UserSession).where(UserSession.user_id == user.id)).all():
            db.delete(session)
        db.delete(user)
        db.commit()
        assert db.exec(select(LearningRun)).all() == []


def test_learn_problems_join_lists_and_camps(local_client):
    """本地练习题可以进题单和集训；完成标记用本人 AC，不进榜单口径。"""
    login(local_client)
    problem_id = str(local_client.get("/api/learning/problems").json()[0]["id"])

    # 题单：正常添加、重复冲突、坏 id 拒绝。
    assert local_client.post("/api/problem-lists", json={"title": "基础练习单"}).status_code == 201
    list_id = local_client.get("/api/problem-lists").json()[0]["id"]
    add = local_client.post(
        f"/api/problem-lists/{list_id}/problems",
        json={"platform": "learn", "key": problem_id},
    )
    assert add.status_code == 201
    entry = add.json()
    assert entry["platform"] == "learn"
    assert entry["name"] and entry["url"].endswith(f"id={problem_id}")
    assert (
        local_client.post(
            f"/api/problem-lists/{list_id}/problems",
            json={"platform": "learn", "key": problem_id},
        ).status_code
        == 409
    )
    assert (
        local_client.post(
            f"/api/problem-lists/{list_id}/problems", json={"platform": "learn", "key": "99999"}
        ).status_code
        == 400
    )

    # 集训：learn 平台不要求绑定账号；加题后可直接报名。
    from datetime import UTC, datetime, timedelta

    from app.db import get_engine
    from app.models import Camp, User

    now = datetime.now(UTC)
    with Session(get_engine()) as db:
        admin = db.exec(select(User).where(User.student_id == "00000000000")).one()
        camp = Camp(
            name="练习集训",
            description="",
            start_at=now - timedelta(hours=1),
            end_at=now + timedelta(hours=1),
            created_by=admin.id,
        )
        db.add(camp)
        db.commit()
        camp_id = camp.id

    assert (
        local_client.post(
            f"/api/admin/camps/{camp_id}/problems",
            json={"platform": "learn", "key": problem_id},
        ).status_code
        == 201
    )
    assert (
        local_client.post(
            f"/api/admin/camps/{camp_id}/problems", json={"platform": "learn", "key": "99999"}
        ).status_code
        == 400
    )
    imported = local_client.post(
        f"/api/admin/camps/{camp_id}/problems/import",
        json={"items": [{"platform": "learn", "key": problem_id}]},
    )
    assert imported.status_code == 200 and imported.json()["skipped"] == 1

    assert local_client.post(f"/api/camps/{camp_id}/register").status_code in (200, 201)
    detail = local_client.get(f"/api/camps/{camp_id}").json()
    assert detail["platforms"] == []  # learn 不是需要绑定的平台
    row = detail["problems"][0]
    assert row["platform"] == "learn" and row["done"] is False
    assert row["url"].endswith(f"id={problem_id}")
    # learn 题进榜（邀请制自报口径），报名后没 AC 前是 0 分。
    assert len(detail["leaderboard"]) == 1
    assert detail["leaderboard"][0]["solved_count"] == 0
    assert detail["leaderboard"][0]["per_problem"][f"learn:{problem_id}"] is False

    # 同步一条自报 AC 后，题单和集训里的完成标记变成 True。
    version = local_client.get(f"/api/learning/problems/{problem_id}").json()["version_id"]
    assert (
        local_client.post(
            "/api/learning/runs",
            json={
                "id": str(uuid4()),
                "version_id": version,
                "environment": "python",
                "runtime_version": "probe",
                "source": "print(42)",
                "verdict": "AC",
            },
        ).status_code
        == 200
    )
    detail = local_client.get(f"/api/camps/{camp_id}").json()
    assert detail["problems"][0]["done"] is True
    assert detail["leaderboard"][0]["solved_count"] == 1
    assert detail["leaderboard"][0]["per_problem"][f"learn:{problem_id}"] is True
    listed = local_client.get(f"/api/problem-lists/{list_id}").json()
    assert listed["problems"][0]["done"] is True
    assert listed["done_count"] == 1
    rank = local_client.get(f"/api/problem-lists/{list_id}/rank").json()
    assert rank["total"] == 0 and rank["entries"] == []


def test_learn_platform_rejected_when_disabled(tmp_path, monkeypatch):
    """SGU_LEARN_ENABLED=0 时题库整块关闭，learn 平台一律拒绝。"""
    monkeypatch.delenv("SGU_LEARN_LOCAL", raising=False)
    monkeypatch.setenv("SGU_LEARN_ENABLED", "0")
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path}/plain.db")
    monkeypatch.setenv("SGU_ACW_COOKIE_SECURE", "false")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", "rootpass123")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", "00000000000")
    from app.config import get_settings
    from app.db import reset_engine

    get_settings.cache_clear()
    reset_engine()
    from app.main import create_app

    try:
        with TestClient(create_app()) as c:
            assert (
                c.post(
                    "/api/auth/login",
                    json={"student_id": "00000000000", "password": "rootpass123"},
                ).status_code
                == 200
            )
            assert c.post("/api/problem-lists", json={"title": "测试单"}).status_code == 201
            list_id = c.get("/api/problem-lists").json()[0]["id"]
            assert (
                c.post(
                    f"/api/problem-lists/{list_id}/problems",
                    json={"platform": "learn", "key": "1"},
                ).status_code
                == 422
            )
    finally:
        reset_engine()
        get_settings.cache_clear()
