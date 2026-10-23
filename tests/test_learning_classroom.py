"""课堂流程：重复提交、账号切换、截止后的记录与并发封榜。"""

# ruff: noqa: F811 -- pytest fixtures are injected by parameter name
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import camps
from app.db import get_engine
from app.learning_models import LearningRun
from app.models import Camp, CampFinalization, Role, User
from app.routers.learning import encoded
from tests.test_learning import local_client, login  # noqa: F401


def receipt(problem, owner, verdict="AC", environment="cpp"):
    return {
        "id": str(uuid4()),
        "version_id": problem["version_id"],
        "environment": environment,
        "runtime_version": "classroom-fixture",
        "source": "int main(){}",
        "verdict": verdict,
        "expected_user_id": owner,
    }


def test_retry_owner_guard_preserves_legacy_receipt_hash(local_client):
    c = local_client
    login(c)
    uid = c.get("/api/auth/me").json()["id"]
    problem = c.get("/api/learning/problems/1").json()
    body = receipt(problem, uid)
    assert (
        c.post("/api/learning/runs", json={**body, "expected_user_id": uid + 1}).status_code == 403
    )
    legacy = {k: v for k, v in body.items() if k != "expected_user_id"}
    digest = sha256(encoded(legacy).encode()).hexdigest()
    with Session(get_engine()) as db:
        assert db.exec(select(LearningRun)).all() == []
        db.add(LearningRun(**legacy, user_id=uid, request_hash=digest))
        db.commit()
    assert c.post("/api/learning/runs", json=body).status_code == 200
    assert c.post("/api/learning/runs", json=legacy).status_code == 200
    with Session(get_engine()) as db:
        rows = db.exec(select(LearningRun)).all()
        assert len(rows) == 1 and rows[0].request_hash == digest


def test_concurrent_retry_of_one_receipt_counts_once(local_client):
    c = local_client
    login(c)
    uid = c.get("/api/auth/me").json()["id"]
    body = receipt(c.get("/api/learning/problems/1").json(), uid)
    barrier = Barrier(4)

    def retry(_):
        barrier.wait()
        return c.post("/api/learning/runs", json=body).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(retry, range(4))) == [200] * 4
    assert len(c.get("/api/learning/runs").json()) == 1
    assert c.get("/api/learning/progress").json() == {"solved_problem_ids": [1]}


def test_classroom_workflow_and_two_session_finalization(local_client):
    c = local_client
    login(c)
    content = c.get("/api/learning/problems/1").json()["content"]
    published = []
    for title in ("课堂题 A", "课堂题 B"):
        draft = c.post(
            "/api/admin/learning/problems",
            json={
                "request_id": str(uuid4()),
                "content": {**content, "title": title},
            },
        ).json()
        published.append(
            c.post(
                f"/api/admin/learning/problems/{draft['id']}/publish",
                json={"revision": draft["revision"]},
            ).json()
        )
    now = datetime.now(UTC)
    camp = c.post(
        "/api/admin/camps",
        json={
            "name": "课堂流程演练",
            "start_at": (now - timedelta(minutes=1)).isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        },
    ).json()
    camp_id = camp["id"]
    base = f"/api/admin/camps/{camp_id}/problems"
    assert (
        c.post(base, json={"platform": "learn", "key": str(published[0]["id"])}).status_code == 201
    )
    imported = c.post(
        base + "/import",
        json={
            "items": [
                {"platform": "learn", "key": str(published[0]["id"]).zfill(4)},
                {"platform": "learn", "key": str(published[1]["id"])},
            ]
        },
    ).json()
    assert imported == {"added": 1, "skipped": 1, "failed": []}
    clients, ids = [], []
    with Session(get_engine()) as db:
        root = db.exec(select(User).where(User.role == Role.ROOT)).one()
        for index in (1, 2):
            member = User(
                **{
                    **root.model_dump(exclude={"id"}),
                    "role": Role.MEMBER,
                    "student_id": f"2412502300{index}",
                    "display_name": f"演练成员{index}",
                }
            )
            db.add(member)
            db.flush()
            ids.append(member.id)
        db.commit()
    guest = TestClient(c.app)
    try:
        for index in (1, 2):
            member = TestClient(c.app)
            clients.append(member)
            assert (
                member.post(
                    "/api/auth/login",
                    json={
                        "student_id": f"2412502300{index}",
                        "password": "LocalLearning123!",
                    },
                ).status_code
                == 200
            )
            assert member.post(f"/api/camps/{camp_id}/register").status_code == 201
            assert member.post(f"/api/camps/{camp_id}/register").status_code == 201
        first = receipt(published[0], ids[0], environment="java8")
        assert clients[0].post("/api/learning/runs", json=first).status_code == 200
        assert clients[0].post("/api/learning/runs", json=first).status_code == 200
        assert (
            clients[1]
            .post("/api/learning/runs", json=receipt(published[0], ids[1], "WA"))
            .status_code
            == 200
        )
        second = receipt(published[1], ids[0], environment="python")
        assert clients[0].post("/api/learning/runs", json=second).status_code == 200
        running = clients[0].get(f"/api/camps/{camp_id}").json()
        assert running["registration_count"] == 2
        assert [r["solved_count"] for r in running["leaderboard"]] == [2, 0]
        assert guest.get(f"/api/camps/{camp_id}").json()["problems"] == []
        # 推进到最后一条正常提交的截止边界，再提交的记录保留在练习进度中。
        with Session(get_engine()) as db:
            actual = db.get(Camp, camp_id)
            actual.end_at = db.get(LearningRun, second["id"]).created_at
            db.add(actual)
            db.commit()
        late = receipt(published[0], ids[1])
        assert clients[1].post("/api/learning/runs", json=late).status_code == 200
        public = guest.get(f"/api/camps/{camp_id}").json()
        assert public["phase"] == "finalizing" and len(public["problems"]) == 2
        assert public["leaderboard"] == [] and all(not p["done"] for p in public["problems"])
        barrier = Barrier(2)

        def finalize(_):
            with Session(get_engine()) as db:
                barrier.wait()
                camps.prepare_finalizations(db)
                camps.finish_finalizations(db)
                db.commit()
                return db.get(CampFinalization, camp_id).standings_json

        with ThreadPoolExecutor(max_workers=2) as pool:
            snapshots = list(pool.map(finalize, range(2)))
        assert snapshots[0] == snapshots[1]
        frozen = clients[1].get(f"/api/camps/{camp_id}").json()
        assert frozen["phase"] == "finalized"
        assert [r["solved_count"] for r in frozen["leaderboard"]] == [2, 0]
        assert all(not p["done"] for p in frozen["problems"])
        assert clients[1].get("/api/learning/progress").json() == {
            "solved_problem_ids": [published[0]["id"]]
        }
        exported = c.get(f"/api/admin/camps/{camp_id}/export?format=json").json()
        assert [r[6:] for r in exported["rows"]] == [["2", "是", "是"], ["0", "否", "否"]]
        assert c.post(base, json={"platform": "learn", "key": "1"}).status_code == 409
        assert clients[0].post(f"/api/camps/{camp_id}/unregister").status_code == 400
        with Session(get_engine()) as db:
            before = db.get(CampFinalization, camp_id).finalized_at
            camps.prepare_finalizations(db)
            camps.finish_finalizations(db)
            db.commit()
            db.expire_all()
            final = db.get(CampFinalization, camp_id)
            assert final.finalized_at == before
            assert json.loads(final.standings_json) == json.loads(snapshots[0])
    finally:
        guest.close()
        for member in clients:
            member.close()
