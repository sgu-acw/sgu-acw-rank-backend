"""集训禁止新增洛谷 U/T 题，批量导入仅跳过这些题。"""

# ruff: noqa: F811 -- pytest fixtures are injected by parameter name
from datetime import timedelta

import pytest
from sqlmodel import select

from app import camps
from app.models import CampProblem, ProblemList, ProblemListItem
from tests.test_camps import add_problem, db, env, make_camp, root_client  # noqa: F401


@pytest.fixture()
def effects(monkeypatch):
    fetched, boosted = [], []
    monkeypatch.setattr(
        "app.routers.camps._fetch_and_cache", lambda _db, p, k: fetched.append((p, k))
    )
    monkeypatch.setattr(camps, "boost_accounts", lambda _db, camp: boosted.append(camp.id))
    return fetched, boosted


@pytest.mark.parametrize("key", ["U123", "t456", "  u00123  ", "T987654"])
def test_single_add_rejects_custom_luogu_problems(root_client, db, effects, key):
    camp = make_camp(db, start_offset=timedelta(days=1))
    response = root_client.post(
        f"/api/admin/camps/{camp.id}/problems", json={"platform": "luogu", "key": key}
    )
    assert response.status_code == 400
    assert "U/T" in response.json()["detail"]
    assert db.exec(select(CampProblem).where(CampProblem.camp_id == camp.id)).all() == []
    assert effects == ([], [])


@pytest.mark.parametrize(
    "platform,key",
    [
        ("luogu", "P1001"),
        ("luogu", "B2063"),
        ("luogu", "UVA100"),
        ("codeforces", "1234T"),
        ("atcoder", "typical90_u"),
    ],
)
def test_standard_problems_and_other_platforms_are_unaffected(
    root_client, db, effects, platform, key
):
    camp = make_camp(db)
    response = root_client.post(
        f"/api/admin/camps/{camp.id}/problems", json={"platform": platform, "key": key}
    )
    assert response.status_code == 201
    assert effects == ([(platform, key)], [camp.id])


def test_mixed_import_skips_ut_and_still_adds_valid_rows(root_client, db, effects):
    camp = make_camp(db)
    add_problem(db, camp)
    url = f"/api/admin/camps/{camp.id}/problems/import"
    payload = {
        "items": [
            {"platform": "luogu", "key": "u123"},
            {"platform": "luogu", "key": "p1001"},
            {"platform": "codeforces", "key": "1A"},
            {"platform": "luogu", "key": "T456"},
            {"platform": "atcoder", "key": "ABC300_A"},
            {"platform": "bogus", "key": "x"},
            {"platform": "luogu", "key": "U123"},
        ]
    }
    response = root_client.post(url, json=payload)
    assert response.status_code == 200
    assert response.json() == {
        "added": 2,
        "skipped": 1,
        "failed": ["bogus:x"],
        "excluded": ["luogu:U123", "luogu:T456"],
    }
    rows = db.exec(
        select(CampProblem).where(CampProblem.camp_id == camp.id).order_by(CampProblem.position)
    ).all()
    assert [(p.platform, p.key, p.position) for p in rows] == [
        ("codeforces", "1A", 0),
        ("luogu", "P1001", 1),
        ("atcoder", "abc300_a", 2),
    ]
    repeated = root_client.post(url, json=payload)
    assert repeated.status_code == 200
    assert repeated.json() == {
        "added": 0,
        "skipped": 3,
        "failed": ["bogus:x"],
        "excluded": ["luogu:U123", "luogu:T456"],
    }
    assert effects == ([("luogu", "P1001"), ("atcoder", "abc300_a")], [camp.id])


@pytest.mark.parametrize("collection", [False, True])
def test_import_filters_problem_lists_and_collections_without_changing_the_source(
    root_client, db, effects, collection
):
    camp = make_camp(db)
    parent = ProblemList(
        kind="collection" if collection else "list", title="来源", description="", position=0
    )
    db.add(parent)
    db.flush()
    lists = [parent]
    if collection:
        lists = [
            ProblemList(
                kind="list", title=f"子题单{i}", description="", position=i, collection_id=parent.id
            )
            for i in range(2)
        ]
        db.add_all(lists)
        db.flush()
    for i, key in enumerate(["u001", "P1002", "T002", "B2063"]):
        db.add(
            ProblemListItem(
                list_id=lists[i // 2].id if collection else parent.id,
                platform="luogu",
                key=key,
                position=i,
            )
        )
    db.commit()
    source_before = [(r.id, r.key, r.list_id) for r in db.exec(select(ProblemListItem)).all()]
    response = root_client.post(
        f"/api/admin/camps/{camp.id}/problems/import", json={"problem_list_id": parent.id}
    )
    assert response.status_code == 200
    assert response.json() == {
        "added": 2,
        "skipped": 0,
        "failed": [],
        "excluded": ["luogu:U001", "luogu:T002"],
    }
    assert [
        (r.id, r.key, r.list_id) for r in db.exec(select(ProblemListItem)).all()
    ] == source_before
    rows = db.exec(
        select(CampProblem).where(CampProblem.camp_id == camp.id).order_by(CampProblem.position)
    ).all()
    assert [(r.key, r.position) for r in rows] == [("P1002", 0), ("B2063", 1)]
    assert effects == ([("luogu", "P1002"), ("luogu", "B2063")], [camp.id])


def test_all_ut_import_returns_a_notice_and_preserves_existing_camp_rows(root_client, db, effects):
    camp = make_camp(db)
    legacy = add_problem(db, camp, platform="luogu", key="U123")
    url = f"/api/admin/camps/{camp.id}/problems/import"
    payload = {"items": [{"platform": "luogu", "key": key} for key in ["u123", "T456", "U123"]]}
    expected = {"added": 0, "skipped": 0, "failed": [], "excluded": ["luogu:U123", "luogu:T456"]}
    for _ in range(2):
        response = root_client.post(url, json=payload)
        assert response.status_code == 200 and response.json() == expected
    db.expire_all()
    rows = db.exec(select(CampProblem).where(CampProblem.camp_id == camp.id)).all()
    assert [(r.id, r.key, r.position) for r in rows] == [(legacy.id, "U123", 0)]
    assert effects == ([], [])
