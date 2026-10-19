"""重复请求、并发名额和状态边界的回归测试，不调用外部平台。"""

# ruff: noqa: F811 -- pytest fixtures are imported for discovery
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.db import get_engine
from app.models import Account, CampRegistration, SignupEntry, User, utcnow
from app.routers.signups import SubmitEntry, submit_entry
from tests.test_camps import (  # noqa: F401
    MEMBER_STUDENT_ID,
    add_problem,
    client,
    db,
    env,
    make_account,
    make_camp,
    member2_client,
    member_client,
    root_client,
    user_id_of,
)


def create_signup(root_client, **overrides):
    payload = {
        "name": "状态测试",
        "open_at": (utcnow() - timedelta(hours=1)).isoformat(),
        "fields": [],
        **overrides,
    }
    response = root_client.post("/api/admin/signups", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_signup_field_identity_survives_reorder_delete_and_add(root_client, member_client):
    signup = create_signup(root_client, fields=[{"label": "姓名"}, {"label": "队名"}])
    base = f"/api/admin/signups/{signup['id']}"
    entry_url = f"/api/signups/{signup['id']}/entries"
    assert (
        member_client.post(
            entry_url, json={"answers": {"field_1": "队员甲", "field_2": "甲队"}}
        ).status_code
        == 200
    )
    fields = signup["fields"]
    swapped = root_client.put(base, json={"fields": list(reversed(fields))}).json()
    assert [f["key"] for f in swapped["fields"]] == ["field_2", "field_1"]
    new_field = {"label": "手机", "key": f"field_{uuid4().hex}"}
    payload = {"fields": [fields[1], new_field]}
    modified = root_client.put(base, json=payload).json()
    assert root_client.put(base, json=payload).json()["fields"] == modified["fields"]
    new_key = modified["fields"][1]["key"]
    assert new_key not in {"field_1", "field_2"}
    answers = root_client.get(base + "/entries").json()[0]["answers"]
    assert answers == {"field_1": "队员甲", "field_2": "甲队"}
    assert (
        member_client.post(
            entry_url, json={"answers": {"field_2": "乙队", new_key: "123"}}
        ).status_code
        == 200
    )
    assert root_client.get(base + "/entries").json()[0]["answers"]["field_1"] == "队员甲"
    assert root_client.put(base, json={"fields": [fields[1], fields[1]]}).status_code == 422
    assert root_client.put(base, json={"fields": [{"label": "无标识"}]}).status_code == 422
    assert root_client.put(base, json={"fields": [fields[1]]}).status_code == 200
    assert root_client.put(base, json=payload).status_code == 422  # 已删字段不能复用旧答案


def test_signup_retry_keeps_identity_and_timestamp(root_client, member_client):
    s = create_signup(root_client, capacity=1)
    url = f"/api/signups/{s['id']}/entries"
    for _ in range(2):
        assert member_client.post(url, json={"answers": {}}).status_code == 200
        rows = root_client.get(f"/api/admin/signups/{s['id']}/entries").json()
        assert len(rows) == 1
        if _ == 0:
            first = rows[0]
        else:
            assert rows == [first]
    assert member_client.delete(url + "/me").status_code == 204
    root_client.put(
        f"/api/admin/signups/{s['id']}",
        json={"close_at": (utcnow() - timedelta(minutes=1)).isoformat()},
    )
    assert member_client.delete(url + "/me").status_code == 204


def test_concurrent_last_signup_slot_is_not_oversold(root_client, member_client, member2_client):
    s = create_signup(root_client, capacity=1)
    ids = [
        member_client.get("/api/auth/me").json()["id"],
        member2_client.get("/api/auth/me").json()["id"],
    ]
    barrier = Barrier(2)

    def attempt(uid):
        with Session(get_engine()) as session:
            user = session.get(User, uid)
            barrier.wait(timeout=5)
            try:
                submit_entry(s["id"], SubmitEntry(answers={}), user, session)
                return 200
            except HTTPException as exc:
                session.rollback()
                return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ids))
    assert sorted(results) == [200, 409]
    assert len(root_client.get(f"/api/admin/signups/{s['id']}/entries").json()) == 1


@pytest.mark.parametrize("answer", [False, "false", "true", 1, None])
def test_required_checkbox_rejects_unchecked_or_non_boolean(root_client, member_client, answer):
    s = create_signup(root_client, fields=[{"label": "同意", "type": "checkbox", "required": True}])
    assert (
        member_client.post(
            f"/api/signups/{s['id']}/entries", json={"answers": {"field_1": answer}}
        ).status_code
        == 422
    )


def test_camp_same_times_and_registration_retry_do_not_reschedule(root_client, member_client, db):
    camp = make_camp(db, start_offset=timedelta(days=1), end_offset=timedelta(days=2))
    add_problem(db, camp)
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    acc = make_account(db, uid, "codeforces", "fixture")
    member_client.post(f"/api/camps/{camp.id}/register")
    db.refresh(acc)
    before = utcnow()
    acc.solved_fetched_at = before
    db.add(acc)
    db.commit()
    response = root_client.put(
        f"/api/admin/camps/{camp.id}",
        json={
            "start_at": camp.start_at.isoformat(),
            "end_at": camp.end_at.isoformat(),
        },
    )
    assert response.status_code == 200
    assert member_client.post(f"/api/camps/{camp.id}/register").status_code == 201
    db.refresh(acc)
    assert acc.solved_fetched_at == before
    assert (
        len(db.exec(select(CampRegistration).where(CampRegistration.camp_id == camp.id)).all()) == 1
    )


def test_running_camp_cannot_return_to_scheduled(root_client, db):
    camp = make_camp(db)
    assert (
        root_client.put(
            f"/api/admin/camps/{camp.id}",
            json={
                "start_at": (utcnow() + timedelta(hours=1)).isoformat(),
            },
        ).status_code
        == 409
    )


def test_import_retry_is_ordered_and_does_not_reschedule(
    root_client, member_client, db, monkeypatch
):
    from app.models import ProblemList, ProblemListItem

    monkeypatch.setattr("app.routers.camps._fetch_and_cache", lambda *args: None)
    camp = make_camp(db)
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    acc = make_account(db, uid, "codeforces", "fixture")
    root_client.post(f"/api/admin/camps/{camp.id}/members", json={"user_id": uid})
    collection = ProblemList(title="合集", kind="collection")
    db.add(collection)
    db.flush()
    for position, keys in [(2, ["1B", "1C"]), (0, ["1A", "1B"])]:
        pl = ProblemList(title="题单", kind="list", collection_id=collection.id, position=position)
        db.add(pl)
        db.flush()
        for index, key in enumerate(keys):
            db.add(ProblemListItem(list_id=pl.id, platform="codeforces", key=key, position=index))
    db.commit()
    url = f"/api/admin/camps/{camp.id}/problems/import"
    first = root_client.post(url, json={"problem_list_id": collection.id})
    assert first.json() == {"added": 3, "skipped": 1, "failed": []}
    db.refresh(acc)
    acc.solved_fetched_at = utcnow()
    db.add(acc)
    db.commit()
    fetched = acc.solved_fetched_at
    second = root_client.post(url, json={"problem_list_id": collection.id})
    assert second.json() == {"added": 0, "skipped": 4, "failed": []}
    db.refresh(acc)
    assert acc.solved_fetched_at == fetched
    assert [
        p["key"] for p in root_client.get(f"/api/admin/camps/{camp.id}").json()["problems"]
    ] == ["1A", "1B", "1C"]


def test_push_retry_returns_receipt_without_resending(root_client, monkeypatch):
    calls = []
    monkeypatch.setattr("app.push.configured", lambda: True)

    def send(*args):
        calls.append(args[2])
        return {"sent": 1, "failed": 0, "dead": 0}

    monkeypatch.setattr("app.push.send_to_user_ids", send)
    payload = {"request_id": str(uuid4()), "title": "fixture", "audience": "all"}
    first = root_client.post("/api/admin/push/announce", json=payload)
    second = root_client.post("/api/admin/push/announce", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(calls) == 1
    changed = root_client.post("/api/admin/push/announce", json={**payload, "body": "changed"})
    assert changed.status_code == 409
    assert len(calls) == 1


def test_push_uncertain_result_is_not_resent(root_client, monkeypatch):
    calls = []
    monkeypatch.setattr("app.push.configured", lambda: True)

    def interrupted(*args):
        calls.append(True)
        raise RuntimeError("interrupted after upstream accepted")

    monkeypatch.setattr("app.push.send_to_user_ids", interrupted)
    payload = {"request_id": str(uuid4()), "title": "fixture"}
    assert root_client.post("/api/admin/push/announce", json=payload).status_code == 500
    assert root_client.post("/api/admin/push/announce", json=payload).status_code == 409
    assert len(calls) == 1


def test_stale_notification_read_does_not_regress_revision(root_client, db):
    from app.models import AdminNotification, NotificationRead
    from app.routers.notifications import mark_read

    n = AdminNotification(fingerprint="fixture", title="状态", body="", revision=3)
    db.add(n)
    db.commit()
    mark_read(db, 1, n)
    db.commit()
    stale = AdminNotification(id=n.id, fingerprint="fixture", title="状态", body="", revision=1)
    mark_read(db, 1, stale)
    db.commit()
    assert db.exec(select(NotificationRead)).one().revision == 3


def test_delete_user_with_new_auth_and_signup_refs(root_client, member_client, db):
    from app.models import (
        LoginTicket,
        PasskeyCredential,
        PushDispatch,
        PushSubscription,
        WebAuthnChallenge,
    )

    uid = member_client.get("/api/auth/me").json()["id"]
    s = create_signup(root_client)
    db.add(SignupEntry(signup_id=s["id"], user_id=uid))
    db.add(PasskeyCredential(user_id=uid, credential_id="fixture", public_key="fixture"))
    db.add(WebAuthnChallenge(challenge="fixture", user_id=uid, expires_at=utcnow()))
    db.add(
        LoginTicket(
            token_hash="fixture", user_id=uid, expires_at=utcnow(), auth_fingerprint="fixture"
        )
    )
    db.add(PushDispatch(request_id="fixture", user_id=uid, payload_hash="fixture"))
    db.add(PushSubscription(user_id=uid, endpoint="https://example.invalid", p256dh="x", auth="x"))
    db.commit()
    assert root_client.delete(f"/api/admin/users/{uid}").status_code == 204
    assert not db.exec(select(Account).where(Account.user_id == uid)).all()


def test_slow_sync_cannot_replace_newer_snapshot(root_client, member_client, db, monkeypatch):
    from app import sync
    from app.models import SolvedSnapshot
    from app.platforms import SolvedSet

    uid = user_id_of(db, MEMBER_STUDENT_ID)
    acc = make_account(db, uid, "luogu", "fixture", solved=["B2063"])

    def fetch(*args):
        # 模拟本次网络请求尚未返回时，另一请求已写入更新的完整快照。
        with Session(get_engine()) as other:
            latest = other.get(Account, acc.id)
            latest.solved_fetched_at = utcnow() + timedelta(seconds=1)
            snapshot = other.exec(
                select(SolvedSnapshot).where(SolvedSnapshot.account_id == acc.id)
            ).one()
            snapshot.problems_json = '["B2063", "B2064"]'
            snapshot.problem_count = 2
            other.add(latest)
            other.add(snapshot)
            other.commit()
        return SolvedSet(platform="luogu", identifier="fixture", problems={"B2063": 0})

    monkeypatch.setattr(sync, "fetch_solved", fetch)
    ok, reason, added = sync.sync_account(db, acc, force=True)
    db.commit()
    assert ok and added == 0 and "忽略旧请求" in reason
    snapshot = db.exec(select(SolvedSnapshot).where(SolvedSnapshot.account_id == acc.id)).one()
    assert "B2064" in snapshot.problems_json


def test_same_post_content_does_not_create_new_popup_version(root_client):
    post = root_client.post("/api/admin/posts", json={"title": "公告测试", "body": "正文"}).json()
    base = f"/api/admin/posts/{post['id']}"
    assert root_client.post(base + "/popup", json={"on": True}).status_code == 200
    before = root_client.get("/api/posts/popup").json()["version"]
    for _ in range(2):
        assert root_client.put(base, json={"title": "公告测试", "body": "正文"}).status_code == 200
        assert root_client.post(base + "/publish", json={"published": True}).status_code == 200
        assert root_client.get("/api/posts/popup").json()["version"] == before
    assert root_client.put(base, json={"sort_order": 10}).status_code == 200
    assert root_client.get("/api/posts/popup").json()["version"] == before


def test_admin_schedule_includes_distant_and_expired_rows(root_client, member_client, db):
    from app.models import WorkshopStatusOverride

    for label, start, until in [("未来", 40, 41), ("过去", -2, -1)]:
        db.add(
            WorkshopStatusOverride(
                label=label,
                start_at=utcnow() + timedelta(days=start),
                until=utcnow() + timedelta(days=until),
                updated_by=1,
            )
        )
    db.commit()
    assert root_client.get("/api/workshop/schedule").json() == []
    assert {r["label"] for r in root_client.get("/api/admin/workshop/schedule").json()} == {
        "未来",
        "过去",
    }
    assert member_client.get("/api/admin/workshop/schedule").status_code == 403
