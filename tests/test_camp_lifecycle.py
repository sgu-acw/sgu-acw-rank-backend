"""集训状态转换、结算持久化和管理边界的回归测试。"""

# ruff: noqa: F811 -- imported pytest fixtures are injected by parameter name
import json
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest
from sqlmodel import select

from app import camps, sync
from app.models import AdminNotification, CampAccountResult, CampFinalization, ProblemCache
from app.platforms import FetchError
from app.platforms import SolvedSet as PlatformSolvedSet
from tests.test_camps import (  # noqa: F401
    MEMBER_STUDENT_ID,
    add_problem,
    client,
    db,
    env,
    make_account,
    make_camp,
    member_client,
    register_user,
    root_client,
    user_id_of,
)

SolvedSet = partial(PlatformSolvedSet, platform="codeforces", identifier="cf")


def ended_camp(db, member_client):
    camp = make_camp(db, start_offset=timedelta(days=-2), end_offset=timedelta(days=-1))
    add_problem(db, camp, key="1A")
    add_problem(db, camp, key="1B", position=1)
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    acc = make_account(db, uid, "codeforces", "cf", solved=[])
    register_user(db, camp, uid)
    return camp, uid, acc


def test_unregistration_time_and_visibility(member_client, db):
    camp = make_camp(db, start_offset=timedelta(hours=1))
    add_problem(db, camp)
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    make_account(db, uid, "codeforces", "cf")
    assert member_client.post(f"/api/camps/{camp.id}/register").status_code == 201
    detail = member_client.get(f"/api/camps/{camp.id}").json()
    assert detail["phase"] == "scheduled"
    assert detail["can_unregister"] is True
    assert detail["problems"] == []
    assert member_client.post(f"/api/camps/{camp.id}/unregister").status_code == 200
    member_client.post(f"/api/camps/{camp.id}/register")
    camp.start_at = datetime.now(UTC) - timedelta(seconds=1)
    db.add(camp)
    db.commit()
    assert member_client.post(f"/api/camps/{camp.id}/unregister").status_code == 400
    detail = member_client.get(f"/api/camps/{camp.id}").json()
    assert detail["registered"] and not detail["can_unregister"]
    assert detail["phase"] == "running"
    assert len(detail["problems"]) == 1


@pytest.mark.parametrize("phase", ["running", "deadline", "pending", "finalized"])
def test_ended_problem_list_is_public_without_exposing_standings(
    phase, client, member_client, root_client, db, monkeypatch
):
    camp = make_camp(db)
    add_problem(db, camp, key="1A", position=1)
    add_problem(db, camp, platform="luogu", key="B2064", position=0)
    db.add(ProblemCache(platform="luogu", key="B2064", name="测试题名"))
    account = make_account(db, 1, "codeforces", "fixture", solved=["1A"])
    register_user(db, camp, 1)
    cutoff = camp.end_at
    now = cutoff - timedelta(microseconds=1) if phase == "running" else cutoff

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(camps, "datetime", Clock)
    if phase in ("pending", "finalized"):
        camps.prepare_finalizations(db)
    if phase == "finalized":
        camps.record_final_sync(
            db, account, SolvedSet(problems={"1A": int(cutoff.timestamp()) - 60}), now
        )
        camps.finish_finalizations(db)
    db.commit()

    for viewer in (client, member_client):
        response = viewer.get(f"/api/camps/{camp.id}")
        assert response.status_code == 200
        detail = response.json()
        assert detail["registered"] is False
        assert detail["leaderboard"] == []
        if phase == "running":
            assert detail["problems"] == []
        else:
            assert detail["ended"] is True
            assert detail["phase"] == ("finalized" if phase == "finalized" else "finalizing")
            assert [p["key"] for p in detail["problems"]] == ["B2064", "1A"]
            assert detail["problems"][0]["name"] == "测试题名"
            assert detail["problems"][0]["url"] == "https://www.luogu.com.cn/problem/B2064"
            assert all(p["done"] is False for p in detail["problems"])
            assert detail["can_register"] is False

    # 已报名成员仍能查看原来的成绩和通过标记。
    participant = root_client.get(f"/api/camps/{camp.id}").json()
    assert participant["registered"] is True
    assert participant["leaderboard"][0]["user_id"] == 1
    assert participant["problems"][1]["done"] is True


def test_admin_added_member_gets_sync_boost(root_client, member_client, db):
    camp = make_camp(db)
    add_problem(db, camp)
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    acc = make_account(db, uid, "codeforces", "cf")
    assert (
        root_client.post(f"/api/admin/camps/{camp.id}/members", json={"user_id": uid}).status_code
        == 201
    )
    db.refresh(acc)
    assert acc.sync_boost_until == camp.end_at
    assert (
        root_client.post(f"/api/admin/camps/{camp.id}/members", json={"user_id": 9999}).status_code
        == 400
    )


@pytest.mark.parametrize(
    "payload", [{"platform": "bad", "key": "1A"}, {"platform": "luogu", "key": "   "}]
)
def test_problem_validation(root_client, db, payload):
    camp = make_camp(db)
    assert root_client.post(f"/api/admin/camps/{camp.id}/problems", json=payload).status_code == 400


def test_duplicate_normalized_problem_and_atomic_reorder(root_client, db, monkeypatch):
    monkeypatch.setattr("app.routers.camps._fetch_and_cache", lambda *args: None)
    camp = make_camp(db)
    a = add_problem(db, camp, key="1A")
    b = add_problem(db, camp, key="1B", position=1)
    foreign = add_problem(db, make_camp(db), key="1C")
    base = f"/api/admin/camps/{camp.id}"
    assert (
        root_client.post(
            f"{base}/problems", json={"platform": "codeforces", "key": " 1a "}
        ).status_code
        == 409
    )
    for ids in [[a.id], [a.id, a.id], [a.id, foreign.id]]:
        assert root_client.post(f"{base}/problems/reorder", json={"ids": ids}).status_code == 409
        assert [p["id"] for p in root_client.get(base).json()["problems"]] == [a.id, b.id]
    assert (
        root_client.post(f"{base}/problems/reorder", json={"ids": [b.id, a.id]}).status_code == 200
    )
    assert [p["id"] for p in root_client.get(base).json()["problems"]] == [b.id, a.id]


def test_naive_time_and_blank_name_rejected(root_client):
    for payload in [
        {"name": "x", "start_at": "2026-10-10T08:00:00", "end_at": "2026-10-10T10:00:00"},
        {
            "name": "  ",
            "start_at": "2026-10-10T08:00:00+08:00",
            "end_at": "2026-10-10T10:00:00+08:00",
        },
    ]:
        assert root_client.post("/api/admin/camps", json=payload).status_code == 422


def test_final_sync_is_durable_and_export_matches(root_client, member_client, db, monkeypatch):
    camp, uid, acc = ended_camp(db, member_client)
    camps.prepare_finalizations(db)
    db.commit()
    # 历史通过计分；截止后的新通过不计入。
    solved = SolvedSet(
        problems={
            "1A": int(camp.end_at.timestamp()) - 86400,
            "1B": int(camp.end_at.timestamp()) + 10,
        }
    )
    monkeypatch.setattr(sync, "fetch_solved", lambda *args: solved)
    sync.sync_account(db, acc, force=True)
    camps.finish_finalizations(db)
    db.commit()
    final = member_client.get(f"/api/camps/{camp.id}").json()
    assert final["phase"] == "finalized"
    assert final["leaderboard"][0]["solved_count"] == 1
    assert [p["done"] for p in final["problems"]] == [True, False]
    # 之后其他页面手动同步、再次运行结算，都不能覆盖封榜成绩。
    solved.problems["1B"] = int(camp.end_at.timestamp()) - 1
    sync.sync_account(db, acc, force=True)
    camps.prepare_finalizations(db)
    camps.finish_finalizations(db)
    db.commit()
    assert member_client.get(f"/api/camps/{camp.id}").json()["leaderboard"] == final["leaderboard"]
    exported = root_client.get(f"/api/admin/camps/{camp.id}/export?format=json").json()
    assert exported["camp"]["phase"] == "finalized"
    assert exported["rows"][0][6:] == ["1", "是", "否"]
    assert len(db.exec(select(CampAccountResult)).all()) == 1


def test_failed_final_sync_remains_pending_then_recovers(member_client, db, monkeypatch):
    camp, uid, acc = ended_camp(db, member_client)
    camps.prepare_finalizations(db)
    result = db.get(CampFinalization, camp.id)
    result.started_at = datetime.now(UTC) - timedelta(minutes=20)
    db.add(result)
    db.commit()

    def fail(*args):
        raise FetchError("upstream unavailable")

    monkeypatch.setattr(sync, "fetch_solved", fail)
    assert sync.sync_account(db, acc, force=True)[0] is False
    camps.finish_finalizations(db)
    db.commit()
    assert camps.result_status(db, camp)["phase"] == "finalizing"
    assert camps.result_status(db, camp)["pending_accounts"] == 1
    assert len(db.exec(select(AdminNotification)).all()) == 2
    # 下一轮 prepare 不能清掉失败时间，避免每轮绕过退避。
    fetched = acc.solved_fetched_at
    camps.prepare_finalizations(db)
    db.refresh(acc)
    assert acc.solved_fetched_at == fetched
    monkeypatch.setattr(sync, "fetch_solved", lambda *args: SolvedSet(problems={"1A": 0}))
    assert sync.sync_account(db, acc, force=True)[0]
    camps.finish_finalizations(db)
    db.commit()
    assert camps.result_status(db, camp)["phase"] == "finalized"
    assert all(n.resolved_at for n in db.exec(select(AdminNotification)).all())


def test_partial_final_results_are_not_overwritten(member_client, db, monkeypatch):
    camp, uid, acc = ended_camp(db, member_client)
    second = make_account(db, uid, "codeforces", "alt")
    camps.prepare_finalizations(db)
    db.commit()
    monkeypatch.setattr(sync, "fetch_solved", lambda *args: SolvedSet(problems={"1A": 0}))
    sync.sync_account(db, acc, force=True)
    db.commit()
    camps.finish_finalizations(db)
    assert camps.result_status(db, camp)["phase"] == "finalizing"
    monkeypatch.setattr(sync, "fetch_solved", lambda *args: SolvedSet(problems={"1B": 0}))
    sync.sync_account(db, acc, force=True)
    sync.sync_account(db, second, force=True)
    camps.finish_finalizations(db)
    db.commit()
    final = camps.standings(db, camp)
    assert final[0]["solved_count"] == 2
    first = db.exec(select(CampAccountResult).where(CampAccountResult.account_id == acc.id)).one()
    assert json.loads(first.solved_json) == ["1A"]


def test_ended_camp_mutations_locked_and_delete_cascades(root_client, member_client, db):
    camp, uid, acc = ended_camp(db, member_client)
    camps.prepare_finalizations(db)
    db.commit()
    base = f"/api/admin/camps/{camp.id}"
    # 已存在的报名可以确认成功，但截止后不能加入新人。
    assert root_client.post(f"{base}/members", json={"user_id": uid}).status_code == 201
    assert root_client.post(f"{base}/members", json={"user_id": 1}).status_code == 400
    assert root_client.delete(f"{base}/members/{uid}").status_code == 409
    assert (
        root_client.post(
            f"{base}/problems", json={"platform": "codeforces", "key": "2A"}
        ).status_code
        == 409
    )
    assert (
        root_client.put(
            base, json={"end_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
        ).status_code
        == 409
    )
    assert root_client.put(base, json={"name": "归档名称"}).status_code == 200
    assert root_client.delete(base).status_code == 204
    assert db.exec(select(CampAccountResult)).all() == []
    assert db.exec(select(CampFinalization)).all() == []


def test_unverified_snapshot_does_not_score(member_client, db):
    camp = make_camp(db)
    add_problem(db, camp)
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    make_account(db, uid, "codeforces", "cf", solved=["1A"], verified=False)
    register_user(db, camp, uid)
    assert camps.leaderboard(db, camp)[0][1] == 0


def test_camp_visibility_and_registration_window(root_client, member_client, db):
    """开放报名/开始显示：相对偏移、绝对时间、公开列表过滤、详情门槛。"""
    base = datetime.now(UTC)

    # 默认相对模式：开始前 3 天开放报名和显示
    resp = root_client.post(
        "/api/admin/camps",
        json={
            "name": "相对模式",
            "start_at": (base + timedelta(days=10)).isoformat(),
            "end_at": (base + timedelta(days=11)).isoformat(),
        },
    )
    assert resp.status_code == 201
    camp = resp.json()
    opens = datetime.fromisoformat(camp["register_open"])
    vis = datetime.fromisoformat(camp["visible_from"])
    start = datetime.fromisoformat(camp["start_at"])
    assert (start - opens).days == 3
    assert (start - vis).days == 3

    # 绝对模式
    abs_open = base + timedelta(days=1)
    abs_vis = base + timedelta(days=2)
    resp = root_client.post(
        "/api/admin/camps",
        json={
            "name": "绝对模式",
            "start_at": (base + timedelta(days=10)).isoformat(),
            "end_at": (base + timedelta(days=11)).isoformat(),
            "register_open_at": abs_open.isoformat(),
            "visible_at": abs_vis.isoformat(),
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert abs(datetime.fromisoformat(body["register_open"]).timestamp() - abs_open.timestamp()) < 2
    assert abs(datetime.fromisoformat(body["visible_from"]).timestamp() - abs_vis.timestamp()) < 2

    # 两种模式同时传 → 422
    assert (
        root_client.post(
            "/api/admin/camps",
            json={
                "name": "冲突",
                "start_at": (base + timedelta(days=10)).isoformat(),
                "end_at": (base + timedelta(days=11)).isoformat(),
                "register_open_at": abs_open.isoformat(),
                "register_open_days": 5,
            },
        ).status_code
        == 422
    )

    # 还没到显示时间的集训：公开列表不含、详情 404、管理员能看到
    far = root_client.post(
        "/api/admin/camps",
        json={
            "name": "未公开",
            "start_at": (base + timedelta(days=10)).isoformat(),
            "end_at": (base + timedelta(days=11)).isoformat(),
            "visible_at": (base + timedelta(days=5)).isoformat(),
        },
    ).json()
    assert not any(c["id"] == far["id"] for c in member_client.get("/api/camps").json())
    assert member_client.get(f"/api/camps/{far['id']}").status_code == 404
    assert any(c["id"] == far["id"] for c in root_client.get("/api/admin/camps").json())

    # 已显示但报名还没开放：can_register 为 False 且 register 400
    not_open = root_client.post(
        "/api/admin/camps",
        json={
            "name": "未开放",
            "start_at": (base + timedelta(days=10)).isoformat(),
            "end_at": (base + timedelta(days=11)).isoformat(),
            "register_open_at": (base + timedelta(days=3)).isoformat(),
            # 让它现在就可见，只留报名窗口没开。
            "visible_at": (base - timedelta(hours=1)).isoformat(),
        },
    ).json()
    detail = member_client.get(f"/api/camps/{not_open['id']}").json()
    assert detail["can_register"] is False
    assert "开放" in detail["register_reason"]
    assert member_client.post(f"/api/camps/{not_open['id']}/register").status_code == 400

    # PATCH 切模式：绝对 → 相对
    patch = root_client.put(
        f"/api/admin/camps/{not_open['id']}",
        json={"register_open_days": 1},
    )
    assert patch.status_code == 200
    new_open = datetime.fromisoformat(patch.json()["register_open"])
    new_start = datetime.fromisoformat(patch.json()["start_at"])
    assert (new_start - new_open).days == 1


def test_timing_fields_locked_after_start(root_client, db):
    """开始/截止后：开放报名、显示时间不能再改；名称说明仍可改。"""
    base = datetime.now(UTC)
    resp = root_client.post(
        "/api/admin/camps",
        json={
            "name": "进行中",
            "start_at": (base - timedelta(hours=1)).isoformat(),
            "end_at": (base + timedelta(days=1)).isoformat(),
        },
    )
    assert resp.status_code == 201
    cid = resp.json()["id"]

    # 已开始：register_open/visible 字段被拒
    for field, value in [
        ("register_open_at", (base + timedelta(hours=1)).isoformat()),
        ("register_open_days", 5),
        ("visible_at", (base + timedelta(hours=1)).isoformat()),
        ("visible_days", 7),
    ]:
        r = root_client.put(f"/api/admin/camps/{cid}", json={field: value})
        assert r.status_code == 409, field
        assert "已开始" in r.json()["detail"]

    # 名称说明仍可以改
    r = root_client.put(f"/api/admin/camps/{cid}", json={"name": "改名"})
    assert r.status_code == 200

    # 截止后：连 start/end 都不能改
    resp2 = root_client.post(
        "/api/admin/camps",
        json={
            "name": "已截止",
            "start_at": (base - timedelta(days=2)).isoformat(),
            "end_at": (base - timedelta(hours=1)).isoformat(),
        },
    )
    ended_id = resp2.json()["id"]
    assert (
        root_client.put(
            f"/api/admin/camps/{ended_id}", json={"end_at": (base + timedelta(days=1)).isoformat()}
        ).status_code
        == 409
    )
