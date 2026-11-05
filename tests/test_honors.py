"""Honor publication boundaries, append-only import, identity ambiguity and write races."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.honor_models import HonorAward, HonorEvent, HonorPerson, HonorRecipient
from app.honors import eligibility, study_end
from app.models import User


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'honors.db'}")
    monkeypatch.setenv("SGU_ACW_COOKIE_SECURE", "false")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", "devpassword123")
    from app.config import get_settings
    from app.db import reset_engine
    from app.main import create_app

    get_settings.cache_clear()
    reset_engine()
    with TestClient(create_app()) as c:
        assert (
            c.post(
                "/api/auth/login", json={"student_id": "00000000000", "password": "devpassword123"}
            ).status_code
            == 200
        )
        yield c
    reset_engine()
    get_settings.cache_clear()


def record(key="srk:test:team1"):
    return {
        "source_key": key,
        "event": {
            "key": "test-2024",
            "title": "测试赛",
            "family": "icpc",
            "occurred_on": "2024-06-01",
        },
        "award": "铜奖",
        "source_url": "https://example.org/results",
        "recipients": ["旧名"],
    }


def member(client, name="同名", enroll_year=2021, **overrides):
    from app.db import get_engine

    with Session(get_engine()) as db:
        count = len(db.exec(select(User)).all())
        u = User(
            student_id=f"21125023{count:03d}",
            display_name=name,
            password_hash="x",
            enroll_year=enroll_year,
            level=1,
            college_code="25",
            major_code="02",
        )
        db.add(u)
        db.commit()
        uid = u.id
    profile = client.get(f"/api/honors/admin/users/{uid}").json()
    if overrides:
        r = client.put(
            f"/api/honors/admin/users/{uid}",
            json={
                "revision": profile["revision"],
                **overrides,
            },
        )
        assert r.status_code == 200, r.text
        profile = r.json()
    return profile


def publish(client, p):
    r = client.post("/api/honors/admin/import", json={"records": [record()], "preview": False})
    assert r.status_code == 200, r.text
    a = client.get("/api/honors/admin/awards").json()[0]
    r = client.put(
        f"/api/honors/admin/awards/{a['id']}",
        json={"revision": a["revision"], "status": "verified", "user_ids": [p["id"]]},
    )
    assert r.status_code == 200, r.text
    return a


def test_calendar_boundaries_and_precision():
    p = User(display_name="甲", enroll_year=2020)
    assert study_end(p) == date(2024, 9, 1)

    def event(d, precision="day"):
        return HonorEvent(
            key="t", title="t", family="icpc", occurred_on=d, date_precision=precision
        )

    assert eligibility(p, event(date(2020, 8, 31))) == "不在在校时间内"
    assert eligibility(p, event(date(2020, 9, 1))) == "计入"
    assert eligibility(p, event(date(2024, 8, 31))) == "计入"
    assert eligibility(p, event(date(2024, 9, 1))) == "不在在校时间内"
    assert eligibility(p, event(date(2024, 1, 1), "year")) == "日期精度不足，需核对边界"
    assert eligibility(p, event(None)) == "待核实比赛日期"
    assert eligibility(None, event(date(2024, 1, 1))) == "待关联用户"
    assert study_end(User(honor_study_start=date(2020, 2, 29))) == date(2024, 2, 29)


def test_import_preview_duplicate_and_conflicting_events(client):
    body = {"records": [record(), record()]}
    assert len(client.post("/api/honors/admin/import", json=body).json()["added"]) == 1
    assert client.get("/api/honors/admin/awards").json() == []
    body["preview"] = False
    r = client.post("/api/honors/admin/import", json=body).json()
    assert len(r["added"]) == len(r["duplicates"]) == 1
    assert client.post("/api/honors/admin/import", json=body).json()["added"] == []
    conflict = record("new-key")
    conflict["event"]["title"] = "不应覆盖已有比赛"
    good = record("another-key")
    r = client.post(
        "/api/honors/admin/import", json={"records": [conflict, good], "preview": False}
    ).json()
    assert len(r["added"]) == len(r["failed"]) == 1
    assert r["failed"][0]["record"]["event"]["title"] == "不应覆盖已有比赛"
    assert all(
        a["event"]["title"] == "测试赛" for a in client.get("/api/honors/admin/awards").json()
    )


def test_no_automatic_name_binding_and_retroactive_exclusion(client):
    p = member(client, name="旧名", former_names=["旧名", "新名"])
    member(client, name="旧名", study_start="2010-09-01")  # names are not identities
    client.post("/api/honors/admin/import", json={"records": [record()], "preview": False})
    a = client.get("/api/honors/admin/awards").json()[0]
    assert a["recipients"][0]["user_id"] is None
    assert client.get("/api/honors").json() == []
    publish(client, p)
    assert len(client.get("/api/honors").json()) == 1
    assert "raw_name" not in client.get("/api/honors").json()[0]["recipients"][0]
    updated = {k: p[k] for k in ["former_names", "study_start", "study_end", "revision"]}
    updated["study_end"] = "2024-06-01"
    assert client.put(f"/api/honors/admin/users/{p['id']}", json=updated).status_code == 200
    assert client.get("/api/honors").json() == []
    assert len(client.get("/api/honors/admin/awards").json()) == 1
    assert client.put(f"/api/honors/admin/users/{p['id']}", json=updated).status_code == 409


def test_verified_public_awards_include_unlinked_names_but_personal_history_does_not(client):
    p = member(client, name="已关联成员")
    client.post("/api/honors/admin/import", json={"records": [record()], "preview": False})
    award = client.get("/api/honors/admin/awards").json()[0]
    assert client.put(
        f"/api/honors/admin/awards/{award['id']}",
        json={"revision": award["revision"], "status": "verified", "user_ids": [None]},
    ).status_code == 200

    public = client.get("/api/honors").json()
    assert len(public) == 1
    assert public[0]["recipients"] == [{"name": "旧名"}]
    assert client.get(f"/api/honors/users/{p['id']}").json() == []


def test_verified_unlinked_future_award_is_not_public(client):
    row = record()
    row["event"]["occurred_on"] = "2099-06-01"
    client.post("/api/honors/admin/import", json={"records": [row], "preview": False})
    award = client.get("/api/honors/admin/awards").json()[0]
    assert client.put(
        f"/api/honors/admin/awards/{award['id']}",
        json={"revision": award["revision"], "status": "verified", "user_ids": [None]},
    ).status_code == 200
    assert client.get("/api/honors").json() == []


def test_personal_history_uses_user_and_keeps_source_after_deletion(client):
    from app.cleanup import delete_user_references
    from app.db import get_engine

    p = member(client, name="新名", former_names=["旧名"])
    publish(client, p)
    assert len(client.get(f"/api/honors/users/{p['id']}").json()) == 1
    public = client.get("/api/honors").json()[0]
    assert public["recipients"] == [{"name": "新名"}]
    with Session(get_engine()) as db:
        delete_user_references(db, p["id"])
        db.delete(db.get(User, p["id"]))
        db.commit()
        recipient = db.exec(select(HonorRecipient)).one()
        assert recipient.user_id is None
        assert recipient.raw_name == "旧名"
    public = client.get("/api/honors").json()
    assert len(public) == 1
    assert public[0]["recipients"] == [{"name": "旧名"}]


def test_name_and_former_name_matching_is_provisional_and_respects_enrollment(client):
    p = member(client, name="新名", former_names=["旧名", "旧名"])
    q = member(client, name="旧名", enroll_year=2010)
    client.post("/api/honors/admin/import", json={"records": [record()], "preview": False})
    r = client.get("/api/honors/admin/awards").json()[0]["recipients"][0]
    assert r["user_id"] is None
    assert r["suggested_user_id"] == p["id"]
    candidates = {c["user_id"]: c for c in r["candidates"]}
    assert candidates[p["id"]]["matched_by"] == "former_name"
    assert candidates[q["id"]]["eligibility"] == "不在在校时间内"
    assert client.get("/api/honors").json() == []
    assert client.get(f"/api/honors/users/{p['id']}").json() == []
    # A second eligible account with the same name leaves the choice to the reviewer.
    member(client, name="旧名")
    r = client.get("/api/honors/admin/awards").json()[0]["recipients"][0]
    assert r["suggested_user_id"] is None
    assert len(r["candidates"]) == 3
    assert p["former_names"] == ["旧名"]


def test_unknown_event_date_does_not_preselect_and_saved_choice_wins(client):
    p = member(client, name="旧名")
    r = record()
    r["event"]["occurred_on"] = None
    client.post("/api/honors/admin/import", json={"records": [r], "preview": False})
    award = client.get("/api/honors/admin/awards").json()[0]
    assert award["recipients"][0]["suggested_user_id"] is None
    assert award["recipients"][0]["candidates"][0]["eligibility"] == "待核实比赛日期"
    result = client.put(
        f"/api/honors/admin/awards/{award['id']}",
        json={
            "revision": award["revision"],
            "status": "pending",
            "user_ids": [p["id"]],
        },
    )
    assert result.status_code == 200
    assert client.get("/api/honors/admin/awards").json()[0]["recipients"][0]["user_id"] == p["id"]


def test_profile_updates_refresh_matching_and_reject_stale_writes(client):
    p = member(client, name="新名")
    client.post("/api/honors/admin/import", json={"records": [record()], "preview": False})
    assert client.get("/api/honors/admin/awards").json()[0]["recipients"][0]["candidates"] == []
    url = f"/api/honors/admin/users/{p['id']}"
    payload = {"revision": p["revision"], "former_names": ["旧名"]}
    assert client.put(url, json=payload).status_code == 200
    assert client.put(url, json=payload).status_code == 409
    assert (
        client.get("/api/honors/admin/awards").json()[0]["recipients"][0]["suggested_user_id"]
        == p["id"]
    )
    # Default enrollment is used when validating an override end date.
    assert (
        client.put(url, json={"revision": p["revision"] + 1, "study_end": "2021-08-31"}).status_code
        == 422
    )
    assert client.get(url).json()["study_end"] is None


def test_admin_permissions_and_validation(client):
    from app.main import create_app

    with TestClient(create_app()) as anonymous:
        assert anonymous.get("/api/honors").status_code == 200
        assert anonymous.get("/api/honors/admin/users").status_code == 401
        assert (
            anonymous.post("/api/honors/admin/import", json={"records": [record()]}).status_code
            == 401
        )
        assert anonymous.get("/api/honors/users/1").status_code == 401
    p = member(client)
    assert (
        client.put(
            f"/api/honors/admin/users/{p['id']}",
            json={
                "revision": p["revision"],
                "former_names": [" "],
            },
        ).status_code
        == 422
    )
    assert (
        client.put(
            f"/api/honors/admin/users/{p['id']}",
            json={
                "revision": p["revision"],
                "study_start": "2020-09-01",
                "study_end": "2020-08-31",
            },
        ).status_code
        == 422
    )
    bad = record()
    bad["source_url"] = "javascript:alert(1)"
    assert client.post("/api/honors/admin/import", json={"records": [bad]}).status_code == 422


def test_two_independent_sessions_append_once(client):
    from app.db import get_engine
    from app.routers.honors import ImportInput, import_awards

    body = ImportInput(records=[record()], preview=False)

    def submit():
        with Session(get_engine()) as db:
            return import_awards(body, db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(len(r["added"]) for r in results) == [0, 1]
    with Session(get_engine()) as db:
        assert len(db.exec(select(HonorAward)).all()) == 1
        assert len(db.exec(select(HonorRecipient)).all()) == 1


def test_competing_reviews_do_not_overwrite(client):
    from fastapi import HTTPException

    from app.db import get_engine
    from app.routers.honors import ReviewInput, review_award

    p = member(client)
    a = publish(client, p)
    revision = client.get("/api/honors/admin/awards").json()[0]["revision"]

    def submit(status):
        with Session(get_engine()) as db:
            try:
                review_award(
                    a["id"], ReviewInput(revision=revision, status=status, user_ids=[p["id"]]), db
                )
                return 200
            except HTTPException as e:
                return e.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, ["excluded", "pending"])) == [200, 409]


def test_event_date_correction_recomputes_and_rejects_stale_writer(client):
    p = member(client)
    publish(client, p)
    a = client.get("/api/honors/admin/awards").json()[0]
    payload = {
        "occurred_on": "2026-06-01",
        "date_precision": "day",
        "expected_occurred_on": "2024-06-01",
        "expected_date_precision": "day",
    }
    url = f"/api/honors/admin/events/{a['event']['id']}/date"
    assert client.put(url, json=payload).status_code == 200
    assert client.get("/api/honors").json() == []
    assert client.put(url, json=payload).status_code == 409


def test_two_profile_writers_preserve_first_save(client):
    from fastapi import HTTPException

    from app.db import get_engine
    from app.routers.honors import UserProfileInput, update_user_profile

    p = member(client)

    def submit(name):
        with Session(get_engine()) as db:
            try:
                update_user_profile(
                    p["id"],
                    UserProfileInput(
                        revision=p["revision"],
                        former_names=[name],
                    ),
                    db,
                )
                return 200
            except HTTPException as e:
                return e.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, ["甲", "乙"])) == [200, 409]


def test_legacy_migration_preserves_explicit_links_but_does_not_guess(client):
    from app.db import get_engine
    from app.migrate import run_migration

    p = member(client, name="站内姓名")
    client.post("/api/honors/admin/import", json={"records": [record()], "preview": False})
    with Session(get_engine()) as db:
        linked = HonorPerson(
            name="荣誉旧名",
            aliases_json='["旧名"]',
            user_id=p["id"],
            study_start=date(2020, 9, 1),
            study_end=date(2025, 9, 1),
        )
        unlinked = HonorPerson(name="站内姓名", study_start=date(2000, 9, 1))
        db.add(linked)
        db.add(unlinked)
        db.flush()
        linked_id, unlinked_id = linked.id, unlinked.id
        r = db.exec(select(HonorRecipient)).one()
        r.person_id = linked.id
        db.add(r)
        db.commit()
    assert run_migration(check_only=True) == 2
    with Session(get_engine()) as db:
        assert db.exec(select(HonorRecipient)).one().user_id is None
    assert run_migration() == 2
    assert run_migration() == 0
    profile = client.get(f"/api/honors/admin/users/{p['id']}").json()
    assert profile["former_names"] == ["旧名", "荣誉旧名"]
    assert profile["study_start"] == "2020-09-01"
    with Session(get_engine()) as db:
        r = db.exec(select(HonorRecipient)).one()
        assert r.user_id == p["id"] and r.person_id is None
        assert db.get(HonorPerson, linked_id).name == "荣誉旧名"
        assert db.get(HonorPerson, unlinked_id).user_id is None
    # Clearing aliases intentionally must survive another startup.
    assert (
        client.put(
            f"/api/honors/admin/users/{p['id']}",
            json={
                "revision": profile["revision"],
                "former_names": [],
            },
        ).status_code
        == 200
    )
    assert run_migration() == 0
    assert client.get(f"/api/honors/admin/users/{p['id']}").json()["former_names"] == []
