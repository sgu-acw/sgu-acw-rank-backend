"""题单：层级约束、完成度计算、权限。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from .conftest import approve_registration

ROOT_STUDENT_ID = "00000000000"
ROOT_PASSWORD = "devpassword123"
MEMBER_STUDENT_ID = "24125023001"
MEMBER_PASSWORD = "xzxze123456"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SGU_ACW_COOKIE_SECURE", "false")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", ROOT_STUDENT_ID)
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", ROOT_PASSWORD)

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()
    yield
    db_module.reset_engine()
    get_settings.cache_clear()


@pytest.fixture()
def root_client(env):
    from app.main import create_app

    with TestClient(create_app()) as c:
        response = c.post(
            "/api/auth/login",
            json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
        )
        assert response.status_code == 200, response.text
        yield c


@pytest.fixture()
def member_client(env, root_client):
    """一个已登录的普通成员。

    注意不能写成 ``with TestClient(...)``：那样会再跑一遍 lifespan，
    而 lifespan 里包含建表与 bootstrap，会把 root_client 已有的会话状态
    搅乱。直接用 TestClient 就行，登录接口不需要 lifespan。
    """
    from app.main import create_app

    invite = root_client.post(
        "/api/admin/invites",
        json={"max_uses": 1, "ttl_hours": 24, "grants_role": "member", "note": ""},
    ).json()

    # 注册**必须**用一个独立的客户端：注册成功会下发新会话的 cookie，
    # 盖掉调用方原来的登录状态。用 root_client 去注册的话，root 会当场
    # 变成刚注册的成员，后面的请求全部 403。
    c = TestClient(create_app())
    reg = c.post(
        "/api/auth/register",
        json={
            "invite_code": invite["code"],
            "student_id": MEMBER_STUDENT_ID,
            "display_name": "测试成员",
            "password": MEMBER_PASSWORD,
        },
    )
    assert reg.status_code == 201, reg.text
    approve_registration(root_client, MEMBER_STUDENT_ID)

    response = c.post(
        "/api/auth/login",
        json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    yield c


def _make_list(client, title="基础题单", kind="list", collection_id=None) -> dict:
    payload = {"kind": kind, "title": title, "description": "", "collection_id": collection_id}
    response = client.post("/api/problem-lists", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _add_problem(client, list_id: int, platform="codeforces", key="1004A") -> dict:
    response = client.post(
        f"/api/problem-lists/{list_id}/problems",
        json={"platform": platform, "key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _mark_solved(user_id: int, platform: str, keys: list[str]) -> None:
    """直接把某人的已过题目塞进快照表，跳过抓取。"""
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import Account, SolvedSnapshot, utcnow

    with Session(get_engine()) as db:
        account = db.exec(
            select(Account).where(Account.user_id == user_id, Account.platform == platform)
        ).first()
        if account is None:
            # verified_at 必须设：完成度排行只列有绑定账号的人，
            # 没验证的账号不算「绑了号」。
            account = Account(
                user_id=user_id,
                platform=platform,
                identifier="tester",
                verified_at=utcnow(),
                created_at=utcnow(),
            )
            db.add(account)
            db.commit()
            db.refresh(account)

        snapshot = db.exec(
            select(SolvedSnapshot).where(SolvedSnapshot.account_id == account.id)
        ).first()
        if snapshot is None:
            snapshot = SolvedSnapshot(account_id=account.id, problems_json="[]")
        snapshot.problems_json = json.dumps(keys)
        snapshot.problem_count = len(keys)
        db.add(snapshot)
        db.commit()


# --------------------------------------------------------------------------- #
# 建与读
# --------------------------------------------------------------------------- #


def test_create_and_read_list(root_client):
    created = _make_list(root_client, "动态规划入门")
    assert created["kind"] == "list"
    assert created["title"] == "动态规划入门"
    assert created["item_count"] == 0

    fetched = root_client.get(f"/api/problem-lists/{created['id']}").json()
    assert fetched["title"] == "动态规划入门"
    assert fetched["problems"] == []


def test_collection_holds_lists(root_client):
    """合集里放题单，顶层同时能看到两者。"""
    collection = _make_list(root_client, "新手路线", kind="collection")
    child = _make_list(root_client, "第一周", collection_id=collection["id"])

    top = root_client.get("/api/problem-lists").json()
    titles = {row["title"] for row in top}
    assert "新手路线" in titles
    assert "第一周" not in titles, "收进合集的题单不该出现在顶层"

    detail = root_client.get(f"/api/problem-lists/{collection['id']}").json()
    assert [c["title"] for c in detail["children"]] == ["第一周"]
    assert detail["children"][0]["id"] == child["id"]


def test_list_without_collection_shows_at_top_level(root_client):
    """不属于任何合集的题单与合集平级显示。"""
    collection = _make_list(root_client, "合集", kind="collection")
    standalone = _make_list(root_client, "散装题单")

    top = root_client.get("/api/problem-lists").json()
    ids = {row["id"] for row in top}
    assert collection["id"] in ids
    assert standalone["id"] in ids


def test_collection_cannot_nest(root_client):
    """只允许两层。合集里不能再放合集。"""
    outer = _make_list(root_client, "外层", kind="collection")
    response = root_client.post(
        "/api/problem-lists",
        json={"kind": "collection", "title": "内层", "collection_id": outer["id"]},
    )
    assert response.status_code == 400
    assert "不能再套" in response.json()["detail"]


def test_cannot_put_list_inside_list(root_client):
    """题单里只能放题目，不能放别的题单。"""
    plain = _make_list(root_client, "题单")
    response = root_client.post(
        "/api/problem-lists",
        json={"kind": "list", "title": "想塞进去", "collection_id": plain["id"]},
    )
    assert response.status_code == 400
    assert "只能放进合集" in response.json()["detail"]


def test_cannot_put_collection_inside_itself(root_client):
    collection = _make_list(root_client, "自引用", kind="collection")
    response = root_client.patch(
        f"/api/problem-lists/{collection['id']}",
        json={"set_collection": True, "collection_id": collection["id"]},
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# 题目
# --------------------------------------------------------------------------- #


def test_add_problem_and_reject_duplicate(root_client):
    lst = _make_list(root_client)
    _add_problem(root_client, lst["id"], key="1004A")

    again = root_client.post(
        f"/api/problem-lists/{lst['id']}/problems",
        json={"platform": "codeforces", "key": "1004A"},
    )
    assert again.status_code == 409


def test_cannot_add_problem_to_collection(root_client):
    collection = _make_list(root_client, "合集", kind="collection")
    response = root_client.post(
        f"/api/problem-lists/{collection['id']}/problems",
        json={"platform": "codeforces", "key": "1004A"},
    )
    assert response.status_code == 400
    assert "合集里只能放题单" in response.json()["detail"]


def test_remove_problem(root_client):
    lst = _make_list(root_client)
    item = _add_problem(root_client, lst["id"])

    assert (
        root_client.delete(f"/api/problem-lists/{lst['id']}/problems/{item['id']}").status_code
        == 204
    )
    assert root_client.get(f"/api/problem-lists/{lst['id']}").json()["problems"] == []


def test_reorder_problems(root_client):
    """拖拽排序按给出的顺序重排。"""
    lst = _make_list(root_client)
    a = _add_problem(root_client, lst["id"], key="1004A")
    b = _add_problem(root_client, lst["id"], key="1005B")
    c = _add_problem(root_client, lst["id"], key="1008A")

    response = root_client.post(
        f"/api/problem-lists/{lst['id']}/problems/reorder",
        json={"ids": [c["id"], a["id"], b["id"]]},
    )
    assert response.status_code == 200, response.text
    assert [p["id"] for p in response.json()] == [c["id"], a["id"], b["id"]]


# --------------------------------------------------------------------------- #
# 完成度
# --------------------------------------------------------------------------- #


def test_progress_needs_login(root_client):
    """未登录不显示进度。谁能过哪些题是个人数据。"""
    lst = _make_list(root_client)
    _add_problem(root_client, lst["id"], key="1004A")

    from app.main import create_app

    anon = TestClient(create_app())
    detail = anon.get(f"/api/problem-lists/{lst['id']}").json()

    assert detail["problems"][0]["done"] is None
    assert detail["done_count"] == 0


def test_progress_counts_solved(root_client, member_client):
    """过了一题，完成度跟着涨。"""
    lst = _make_list(root_client)
    _add_problem(root_client, lst["id"], key="1004A")
    _add_problem(root_client, lst["id"], key="1005B")

    members = root_client.get("/api/admin/users").json()["items"]
    member = next(u for u in members if u["student_id"] == MEMBER_STUDENT_ID)
    _mark_solved(member["id"], "codeforces", ["1004A"])

    detail = member_client.get(f"/api/problem-lists/{lst['id']}").json()
    assert detail["done_count"] == 1
    assert detail["total_problems"] == 2
    assert detail["problems"][0]["done"] is True
    assert detail["problems"][1]["done"] is False


def test_collection_progress_is_sum_of_children(root_client, member_client):
    """合集的完成度是下面所有题单的合计，不是「几个子题单做完了」。"""
    collection = _make_list(root_client, "合集", kind="collection")
    first = _make_list(root_client, "第一周", collection_id=collection["id"])
    second = _make_list(root_client, "第二周", collection_id=collection["id"])

    _add_problem(root_client, first["id"], key="1004A")
    _add_problem(root_client, first["id"], key="1005B")
    _add_problem(root_client, second["id"], key="1008A")

    members = root_client.get("/api/admin/users").json()["items"]
    member = next(u for u in members if u["student_id"] == MEMBER_STUDENT_ID)
    _mark_solved(member["id"], "codeforces", ["1004A", "1008A"])

    detail = member_client.get(f"/api/problem-lists/{collection['id']}").json()
    assert detail["total_problems"] == 3
    assert detail["done_count"] == 2
    assert detail["item_count"] == 2, "item_count 是子题单数量"


def test_main_and_alt_accounts_are_merged(root_client, member_client):
    """主号小号合并计算：任一账号过了就算过了。

    与练习量榜的口径一致，否则同一个人在两个页面的进度对不上。
    """
    from sqlmodel import Session

    from app.db import get_engine
    from app.models import Account, SolvedSnapshot, utcnow

    lst = _make_list(root_client)
    _add_problem(root_client, lst["id"], key="1004A")

    members = root_client.get("/api/admin/users").json()["items"]
    member = next(u for u in members if u["student_id"] == MEMBER_STUDENT_ID)

    # 主号过了一题，小号过了另一题；题单里只有第一题。
    _mark_solved(member["id"], "codeforces", ["1004A"])

    with Session(get_engine()) as db:
        small = Account(
            user_id=member["id"],
            platform="codeforces",
            identifier="small-account",
            created_at=utcnow(),
        )
        db.add(small)
        db.commit()
        db.refresh(small)
        db.add(
            SolvedSnapshot(
                account_id=small.id,
                problems_json=json.dumps(["9999Z"]),
                problem_count=1,
            )
        )
        db.commit()

    detail = member_client.get(f"/api/problem-lists/{lst['id']}").json()
    assert detail["done_count"] == 1


# --------------------------------------------------------------------------- #
# 权限
# --------------------------------------------------------------------------- #


def test_member_cannot_write(root_client, member_client):
    """成员能看题单，但不能改。"""
    lst = _make_list(root_client)

    assert (
        member_client.post("/api/problem-lists", json={"kind": "list", "title": "越权"}).status_code
        == 403
    )
    assert (
        member_client.patch(f"/api/problem-lists/{lst['id']}", json={"title": "改"}).status_code
        == 403
    )
    assert member_client.delete(f"/api/problem-lists/{lst['id']}").status_code == 403
    assert (
        member_client.post(
            f"/api/problem-lists/{lst['id']}/problems",
            json={"platform": "codeforces", "key": "1004A"},
        ).status_code
        == 403
    )


def test_anonymous_can_read(root_client):
    """未登录也能看题单，只是没有进度。"""
    lst = _make_list(root_client, "公开题单")

    from app.main import create_app

    anon = TestClient(create_app())
    assert anon.get("/api/problem-lists").status_code == 200
    assert anon.get(f"/api/problem-lists/{lst['id']}").status_code == 200


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #


def test_deleting_collection_keeps_children(root_client):
    """删合集不删里面的题单，只是让它们失去归属。

    合集常常只是分组，删分组不该把内容一起带走。
    """
    collection = _make_list(root_client, "分组", kind="collection")
    child = _make_list(root_client, "里面的题单", collection_id=collection["id"])

    assert root_client.delete(f"/api/problem-lists/{collection['id']}").status_code == 204

    survived = root_client.get(f"/api/problem-lists/{child['id']}")
    assert survived.status_code == 200
    assert survived.json()["collection_id"] is None


def test_deleting_list_removes_its_problems(root_client):
    lst = _make_list(root_client)
    item = _add_problem(root_client, lst["id"])

    assert root_client.delete(f"/api/problem-lists/{lst['id']}").status_code == 204
    assert (
        root_client.delete(f"/api/problem-lists/{lst['id']}/problems/{item['id']}").status_code
        == 404
    )


def test_include_nested_returns_collection_children(root_client):
    """管理页需要看到合集内部的题单，否则没法给它们加题。

    默认只返回顶层，那是对的——展示页不该把合集里的题单再列一遍。
    但管理页必须能看到全部，所以留了 include_nested。
    """
    collection = _make_list(root_client, "分组", kind="collection")
    child = _make_list(root_client, "里面的题单", collection_id=collection["id"])

    top_only = root_client.get("/api/problem-lists").json()
    assert child["id"] not in {row["id"] for row in top_only}

    everything = root_client.get("/api/problem-lists?include_nested=true").json()
    assert child["id"] in {row["id"] for row in everything}
    assert collection["id"] in {row["id"] for row in everything}


# --------------------------------------------------------------------------- #
# 完成度排行榜
# --------------------------------------------------------------------------- #


def test_completion_rank_orders_by_done_count(root_client, member_client):
    """完成度排行榜按过了多少题排序。"""
    lst = _make_list(root_client, "入门五题")
    for key in ["4A", "71A", "158A", "1A", "231A"]:
        _add_problem(root_client, lst["id"], key=key)

    members = root_client.get("/api/admin/users").json()["items"]
    member = next(u for u in members if u["student_id"] == MEMBER_STUDENT_ID)
    _mark_solved(member["id"], "codeforces", ["4A", "71A", "158A"])

    board = root_client.get(f"/api/problem-lists/{lst['id']}/rank").json()
    assert board["total"] == 5
    assert len(board["entries"]) == 1

    entry = board["entries"][0]
    assert entry["done_count"] == 3
    assert entry["percent"] == 60
    assert entry["class_label"] == "信工软工 3 班"


def test_completion_rank_only_lists_members_with_accounts(root_client):
    """没绑账号的人不进榜。

    他们完成度恒为 0，列进来会淹没真正在练的人，也会让人以为「大家都没开始」。
    """
    from app.main import create_app

    invite = root_client.post(
        "/api/admin/invites",
        json={"max_uses": 1, "ttl_hours": 24, "grants_role": "member", "note": ""},
    ).json()
    TestClient(create_app()).post(
        "/api/auth/register",
        json={
            "invite_code": invite["code"],
            "student_id": "25125023030",
            "display_name": "没绑号的",
            "password": MEMBER_PASSWORD,
        },
    )

    lst = _make_list(root_client, "只有绑号的人")
    _add_problem(root_client, lst["id"], key="4A")

    board = root_client.get(f"/api/problem-lists/{lst['id']}/rank").json()
    names = {e["display_name"] for e in board["entries"]}
    assert "没绑号的" not in names


def test_completion_rank_of_empty_list(root_client):
    """空题单不报错，返回空榜。"""
    lst = _make_list(root_client, "空题单")
    board = root_client.get(f"/api/problem-lists/{lst['id']}/rank").json()
    assert board["total"] == 0
    assert board["entries"] == []


def test_completion_rank_of_missing_list(root_client):
    assert root_client.get("/api/problem-lists/99999/rank").status_code == 404


def test_purge_problem_cache(root_client, env):
    """清缓存接口把对应平台的缓存删掉。"""
    from app.db import get_engine
    from app.models import ProblemCache

    with Session(get_engine()) as session:
        session.add(ProblemCache(platform="nowcoder", key="nowcoder:contest:1", name="甲"))
        session.add(ProblemCache(platform="luogu", key="P1001", name="乙"))
        session.commit()

    response = root_client.post("/api/problem-lists/purge-problem-cache?platform=nowcoder")
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 1

    with Session(get_engine()) as session:
        left = session.exec(select(ProblemCache)).all()
    assert [(r.platform, r.key) for r in left] == [("luogu", "P1001")]


def test_purge_problem_cache_rejects_unknown_platform(root_client, env):
    response = root_client.post("/api/problem-lists/purge-problem-cache?platform=nope")
    assert response.status_code == 400


def test_purge_problem_cache_requires_admin(member_client, env):
    response = member_client.post("/api/problem-lists/purge-problem-cache")
    assert response.status_code == 403
