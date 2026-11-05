"""Honor publication boundaries, append-only import, identity ambiguity and write races."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.honor_models import HonorAward, HonorEvent, HonorPerson, HonorRecipient
from app.honors import eligibility, study_end


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


def person(client, **overrides):
    data = {"name": "同名", "study_start": "2021-09-01", **overrides}
    r = client.post("/api/honors/admin/people", json=data)
    assert r.status_code == 200, r.text
    return r.json()


def publish(client, p):
    r = client.post("/api/honors/admin/import", json={"records": [record()], "preview": False})
    assert r.status_code == 200, r.text
    a = client.get("/api/honors/admin/awards").json()[0]
    r = client.put(
        f"/api/honors/admin/awards/{a['id']}",
        json={"revision": a["revision"], "status": "verified", "person_ids": [p["id"]]},
    )
    assert r.status_code == 200, r.text
    return a


def test_calendar_boundaries_and_precision():
    p = HonorPerson(name="甲", study_start=date(2020, 9, 1))
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
    assert eligibility(None, event(date(2024, 1, 1))) == "待关联身份"
    assert study_end(HonorPerson(name="闰年", study_start=date(2020, 2, 29))) == date(2024, 2, 29)


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
    p = person(client, name="旧名", aliases=["旧名", "新名"])
    person(client, name="旧名", study_start="2010-09-01")  # names are not identities
    client.post("/api/honors/admin/import", json={"records": [record()], "preview": False})
    a = client.get("/api/honors/admin/awards").json()[0]
    assert a["recipients"][0]["person_id"] is None
    assert client.get("/api/honors").json() == []
    publish(client, p)
    assert len(client.get("/api/honors").json()) == 1
    assert "raw_name" not in client.get("/api/honors").json()[0]["recipients"][0]
    updated = {
        k: p[k]
        for k in ["name", "aliases", "user_id", "study_start", "study_end", "note", "revision"]
    }
    updated["study_end"] = "2024-06-01"
    assert client.put(f"/api/honors/admin/people/{p['id']}", json=updated).status_code == 200
    assert client.get("/api/honors").json() == []
    assert len(client.get("/api/honors/admin/awards").json()) == 1
    assert client.put(f"/api/honors/admin/people/{p['id']}", json=updated).status_code == 409


def test_personal_history_requires_explicit_link_and_preserves_after_deletion(client):
    from app.cleanup import delete_user_references
    from app.db import get_engine
    from app.models import User

    with Session(get_engine()) as db:
        u = User(
            student_id="24125023001",
            display_name="旧名",
            password_hash="x",
            enroll_year=2024,
            level=1,
            college_code="25",
            major_code="02",
        )
        db.add(u)
        db.commit()
        uid = u.id
    p = person(client, name="新名", aliases=["旧名"], user_id=uid)
    publish(client, p)
    assert len(client.get(f"/api/honors/users/{uid}").json()) == 1
    public = client.get("/api/honors").json()[0]
    assert "user_id" not in public["recipients"][0]
    with Session(get_engine()) as db:
        delete_user_references(db, uid)
        db.delete(db.get(User, uid))
        db.commit()
        assert db.get(HonorPerson, p["id"]).user_id is None
        assert len(db.exec(select(HonorRecipient)).all()) == 1
    assert len(client.get("/api/honors").json()) == 1


def test_reserved_merge_preflight_reports_conflicts(client):
    p, q = person(client), person(client, study_start="2010-09-01")
    r = client.get(f"/api/honors/admin/people/{p['id']}/merge-preview?target_id={q['id']}")
    assert not r.json()["executable"]
    assert "在校区间不同" in r.json()["conflicts"]


def test_admin_permissions_and_validation(client):
    from app.main import create_app

    with TestClient(create_app()) as anonymous:
        assert anonymous.get("/api/honors").status_code == 200
        assert anonymous.get("/api/honors/admin/people").status_code == 401
        assert (
            anonymous.post("/api/honors/admin/import", json={"records": [record()]}).status_code
            == 401
        )
        assert anonymous.get("/api/honors/users/1").status_code == 401
    assert (
        client.post("/api/honors/admin/people", json={"name": " ", "study_start": None}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/honors/admin/people",
            json={"name": "甲", "study_start": "2020-09-01", "study_end": "2020-08-31"},
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

    p = person(client)
    a = publish(client, p)
    revision = client.get("/api/honors/admin/awards").json()[0]["revision"]

    def submit(status):
        with Session(get_engine()) as db:
            try:
                review_award(
                    a["id"], ReviewInput(revision=revision, status=status, person_ids=[p["id"]]), db
                )
                return 200
            except HTTPException as e:
                return e.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, ["excluded", "pending"])) == [200, 409]


def test_event_date_correction_recomputes_and_rejects_stale_writer(client):
    p = person(client)
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
