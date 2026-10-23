"""题库接入集训、截止计分和完整学习进度的回归。"""

# ruff: noqa: F811 -- pytest fixtures are injected by parameter name
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlmodel import Session, select

from app import camps
from app.db import get_engine
from app.learning_models import LearningRun, LearningVersion
from app.models import Camp, CampFinalization, CampProblem, CampRegistration, ProblemCache, User
from tests.test_learning import local_client, login  # noqa: F401


def make_learning_camp(client, problem_ids=(1,), *, external=False):
    login(client)
    now = datetime.now(UTC)
    with Session(get_engine()) as db:
        user = db.exec(select(User).where(User.student_id == "00000000000")).one()
        camp = Camp(
            name="题库集训回归",
            description="",
            created_by=user.id,
            start_at=now - timedelta(hours=1),
            end_at=now + timedelta(hours=1),
        )
        db.add(camp)
        db.flush()
        for i, problem_id in enumerate(problem_ids):
            db.add(CampProblem(camp_id=camp.id, platform="learn", key=str(problem_id), position=i))
        if external:
            db.add(CampProblem(camp_id=camp.id, platform="codeforces", key="1A", position=10))
        db.commit()
        return camp.id, user.id, camp.end_at


def add_run(db, user_id, problem_id, created_at, verdict="AC"):
    version = db.exec(
        select(LearningVersion).where(LearningVersion.problem_id == problem_id)
    ).first()
    run = LearningRun(
        id=str(uuid4()),
        user_id=user_id,
        version_id=version.id,
        environment="python",
        runtime_version="audit",
        source="print(42)",
        verdict=verdict,
        request_hash=str(uuid4()),
        created_at=created_at,
    )
    db.add(run)


def test_learning_deadline_finalization_and_done_marks_are_consistent(local_client, monkeypatch):
    c = local_client
    camp_id, uid, cutoff = make_learning_camp(c, (1, 2, 3))
    assert c.post(f"/api/camps/{camp_id}/register").status_code == 201
    with Session(get_engine()) as db:
        add_run(db, uid, 1, cutoff - timedelta(days=10))  # 历史通过计分。
        add_run(db, uid, 2, cutoff)  # 截止边界计分。
        add_run(db, uid, 3, cutoff + timedelta(microseconds=1))  # 截止后不计分。
        db.commit()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cutoff + timedelta(seconds=1)

    monkeypatch.setattr(camps, "datetime", Clock)
    pending = c.get(f"/api/camps/{camp_id}").json()
    assert pending["phase"] == "finalizing"
    assert pending["leaderboard"][0]["solved_count"] == 2
    assert [p["done"] for p in pending["problems"]] == [True, True, False]

    with Session(get_engine()) as db:
        camps.prepare_finalizations(db)
        result = db.get(CampFinalization, camp_id)
        assert json.loads(result.problems_json) == ["learn:1", "learn:2", "learn:3"]
        camps.finish_finalizations(db)
        db.commit()
    frozen = c.get(f"/api/camps/{camp_id}").json()
    assert frozen["phase"] == "finalized"
    assert frozen["leaderboard"] == pending["leaderboard"]
    assert [p["done"] for p in frozen["problems"]] == [True, True, False]
    export = c.get(f"/api/admin/camps/{camp_id}/export?format=json").json()
    assert export["rows"][0][6:] == ["2", "是", "是", "否"]

    with Session(get_engine()) as db:
        add_run(db, uid, 3, cutoff + timedelta(minutes=1))
        db.commit()
        camps.prepare_finalizations(db)
        camps.finish_finalizations(db)
        db.commit()
    unchanged = c.get(f"/api/camps/{camp_id}").json()
    assert unchanged["leaderboard"] == frozen["leaderboard"]
    assert [p["done"] for p in unchanged["problems"]] == [True, True, False]

    # 有学习记录但未报名的人，不获得集训通过标记。
    with Session(get_engine()) as db:
        registration = db.exec(
            select(CampRegistration).where(CampRegistration.camp_id == camp_id)
        ).one()
        db.delete(registration)
        db.commit()
    outsider = c.get(f"/api/camps/{camp_id}").json()
    assert outsider["registered"] is False and outsider["leaderboard"] == []
    assert all(not p["done"] for p in outsider["problems"])
    c.cookies.clear()
    assert all(not p["done"] for p in c.get(f"/api/camps/{camp_id}").json()["problems"])


def test_learning_allows_registration_in_a_mixed_camp_without_external_accounts(local_client):
    c = local_client
    mixed_id, _, _ = make_learning_camp(c, external=True)
    detail = c.get(f"/api/camps/{mixed_id}").json()
    assert detail["can_register"] is True
    assert "codeforces" in detail["platforms"]
    assert c.post(f"/api/camps/{mixed_id}/register").status_code == 201
    external_id, _, _ = make_learning_camp(c, (), external=True)
    assert c.post(f"/api/camps/{external_id}/register").status_code == 400


def test_imported_learning_metadata_is_committed(local_client):
    c = local_client
    camp_id, _, _ = make_learning_camp(c, ())
    with Session(get_engine()) as db:
        for row in db.exec(select(ProblemCache).where(ProblemCache.platform == "learn")).all():
            db.delete(row)
        db.commit()
    response = c.post(
        f"/api/admin/camps/{camp_id}/problems/import",
        json={"items": [{"platform": "learn", "key": "1"}]},
    )
    assert response.status_code == 200 and response.json()["added"] == 1
    detail = c.get(f"/api/admin/camps/{camp_id}").json()
    assert detail["problems"][0]["name"] == "A + B"


def test_progress_keeps_ac_older_than_the_recent_run_window(local_client):
    c = local_client
    login(c)
    now = datetime.now(UTC)
    with Session(get_engine()) as db:
        uid = db.exec(select(User.id).where(User.student_id == "00000000000")).one()
        add_run(db, uid, 1, now - timedelta(days=1))
        for i in range(105):
            add_run(db, uid, 2, now + timedelta(seconds=i), "WA")
        db.commit()
    recent = c.get("/api/learning/runs").json()
    assert len(recent) == 100 and all(r["verdict"] == "WA" for r in recent)
    progress = c.get("/api/learning/progress")
    assert progress.status_code == 200
    assert progress.json() == {"solved_problem_ids": [1]}
    c.cookies.clear()
    assert c.get("/api/learning/progress").status_code == 401


def test_learning_versions_and_unpublished_camp_entries(local_client):
    c = local_client
    camp_id, uid, _ = make_learning_camp(c, ())
    content = c.get("/api/learning/problems/1").json()["content"]
    draft = c.post(
        "/api/admin/learning/problems",
        json={
            "request_id": str(uuid4()),
            "content": {**content, "title": "尚未发布"},
        },
    ).json()
    assert draft["id"] not in [p["id"] for p in c.get("/api/learning/problems").json()]
    base = f"/api/admin/camps/{camp_id}/problems"
    assert c.post(base, json={"platform": "learn", "key": str(draft["id"])}).status_code == 400
    assert c.post(base, json={"platform": "learn", "key": "0001"}).status_code == 201
    assert c.post(base, json={"platform": "learn", "key": "1"}).status_code == 409
    imported = c.post(
        base + "/import",
        json={
            "items": [
                {"platform": "learn", "key": "001"},
                {"platform": "learn", "key": str(draft["id"])},
            ]
        },
    ).json()
    assert imported == {"added": 0, "skipped": 1, "failed": [f"learn:{draft['id']}"]}
    assert c.post(f"/api/camps/{camp_id}/register").status_code == 201
    with Session(get_engine()) as db:
        add_run(db, uid, 1, datetime.now(UTC))
        db.commit()
    saved = c.put(
        "/api/admin/learning/problems/1",
        json={
            "revision": 1,
            "content": {**content, "title": "新发布题名"},
        },
    ).json()
    assert (
        c.post(
            "/api/admin/learning/problems/1/publish", json={"revision": saved["revision"]}
        ).status_code
        == 200
    )
    detail = c.get(f"/api/camps/{camp_id}").json()
    assert detail["problems"][0]["name"] == "新发布题名"
    assert detail["problems"][0]["done"] is True
    assert detail["leaderboard"][0]["solved_count"] == 1
    assert c.get("/api/learning/progress").json() == {"solved_problem_ids": [1]}


def test_pending_legacy_finalization_fills_learning_before_its_first_freeze(
    local_client, monkeypatch
):
    camp_id, uid, cutoff = make_learning_camp(local_client, (1, 2))
    with Session(get_engine()) as db:
        add_run(db, uid, 1, cutoff - timedelta(seconds=1))
        db.add(
            CampFinalization(
                camp_id=camp_id,
                problems_json="[]",
                participants_json=json.dumps([{"user_id": uid, "student_id": "00000000000"}]),
            )
        )
        db.commit()

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cutoff + timedelta(seconds=1)

        monkeypatch.setattr(camps, "datetime", Clock)
        camps.finish_finalizations(db)
        db.commit()
        db.expire_all()
        final = db.get(CampFinalization, camp_id)
        assert final.finalized_at is not None
        assert json.loads(final.problems_json) == ["learn:1", "learn:2"]
        assert json.loads(final.standings_json)[0]["solved_count"] == 1


def test_mixed_camp_finalizes_learning_and_external_results_together(local_client, monkeypatch):
    from app.platforms import SolvedSet
    from tests.test_camps import make_account

    c = local_client
    camp_id, uid, cutoff = make_learning_camp(c, external=True)
    assert c.post(f"/api/camps/{camp_id}/register").status_code == 201
    with Session(get_engine()) as db:
        account = make_account(db, uid, "codeforces", "audit-account")
        add_run(db, uid, 1, cutoff - timedelta(days=1))
        db.commit()

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cutoff + timedelta(seconds=1)

        monkeypatch.setattr(camps, "datetime", Clock)
        camps.prepare_finalizations(db)
        camps.finish_finalizations(db)
        db.commit()
        camp = db.get(Camp, camp_id)
        assert camps.result_status(db, camp)["pending_accounts"] == 1
        assert camps.result_status(db, camp)["phase"] == "finalizing"
        camps.record_final_sync(
            db,
            account,
            SolvedSet(
                platform="codeforces",
                identifier="audit-account",
                problems={"1A": int(cutoff.timestamp()) - 1},
            ),
            cutoff + timedelta(seconds=1),
        )
        camps.finish_finalizations(db)
        db.commit()
    detail = c.get(f"/api/camps/{camp_id}").json()
    assert detail["phase"] == "finalized"
    assert detail["leaderboard"][0]["solved_count"] == 2
    assert detail["leaderboard"][0]["per_problem"] == {"learn:1": True, "codeforces:1A": True}
