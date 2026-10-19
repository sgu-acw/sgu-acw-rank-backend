"""集训的测试。

覆盖：管理员建改删、用户报名（账号校验、时间限制）、排行榜、导出、
报名账号的同步加速。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import camps
from app.models import Account, Camp, CampProblem

ROOT_STUDENT_ID = "00000000000"
ROOT_PASSWORD = "devpassword123"
MEMBER_STUDENT_ID = "24125023001"
MEMBER_PASSWORD = "xzxze123456"
MEMBER2_STUDENT_ID = "23125024002"
MEMBER2_PASSWORD = "abc1234567"


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
    db_module.init_db()
    yield
    db_module.reset_engine()
    get_settings.cache_clear()


@pytest.fixture()
def db(env):
    from app.db import get_engine

    with Session(get_engine()) as session:
        yield session


@pytest.fixture()
def client(env):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


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


def _login(client_c: TestClient, student_id: str, password: str) -> TestClient:
    response = client_c.post(
        "/api/auth/login", json={"student_id": student_id, "password": password}
    )
    assert response.status_code == 200, response.text
    return client_c


@pytest.fixture()
def member_client(env, root_client):
    """一个可登录的普通成员客户端。账号由管理员接口建，密码是真的。"""
    response = root_client.post(
        "/api/admin/users",
        json={
            "student_id": MEMBER_STUDENT_ID,
            "password": MEMBER_PASSWORD,
            "display_name": "队员甲",
        },
    )
    assert response.status_code in (200, 201), response.text

    from app.main import create_app

    with TestClient(create_app()) as c:
        _login(c, MEMBER_STUDENT_ID, MEMBER_PASSWORD)
        yield c


@pytest.fixture()
def member2_client(env, root_client):
    """第二个成员，排行榜要两人以上才有意义。"""
    response = root_client.post(
        "/api/admin/users",
        json={
            "student_id": MEMBER2_STUDENT_ID,
            "password": MEMBER2_PASSWORD,
            "display_name": "队员乙",
        },
    )
    assert response.status_code in (200, 201), response.text

    from app.main import create_app

    with TestClient(create_app()) as c:
        _login(c, MEMBER2_STUDENT_ID, MEMBER2_PASSWORD)
        yield c


# --------------------------------------------------------------------------- #
# 直接操作库的造数工具
# --------------------------------------------------------------------------- #


def make_camp(
    session: Session,
    *,
    name: str = "测试集训",
    start_offset: timedelta = timedelta(hours=-1),
    end_offset: timedelta = timedelta(hours=23),
) -> Camp:
    """造一份集训。默认已经开始、还没结束。"""
    from app.models import Camp

    now = datetime.now(UTC)
    camp = Camp(
        name=name,
        description="",
        start_at=now + start_offset,
        end_at=now + end_offset,
        created_by=1,
    )
    session.add(camp)
    session.commit()
    session.refresh(camp)
    return camp


def add_problem(
    session: Session,
    camp: Camp,
    platform: str = "codeforces",
    key: str = "1A",
    position: int = 0,
) -> CampProblem:
    from app.models import CampProblem

    row = CampProblem(
        camp_id=camp.id, platform=platform, key=key, position=position
    )
    session.add(row)
    session.commit()
    return row


def user_id_of(session: Session, student_id: str) -> int:
    from app.models import User

    user = session.exec(select(User).where(User.student_id == student_id)).first()
    assert user is not None, f"没有学号 {student_id} 的用户"
    return user.id or 0


def make_account(
    session: Session,
    user_id: int,
    platform: str,
    identifier: str,
    solved: list[str] | None = None,
    verified: bool = True,
) -> Account:
    """造一个平台账号，可选带过题快照。"""
    from app.models import Account, SolvedSnapshot

    acc = Account(
        user_id=user_id,
        platform=platform,
        identifier=identifier,
        verified_at=datetime.now(UTC) if verified else None,
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    if solved is not None:
        session.add(
            SolvedSnapshot(account_id=acc.id, problems_json=json.dumps(solved))
        )
        session.commit()
    return acc


def register_user(session: Session, camp: Camp, user_id: int) -> None:
    from app.models import CampRegistration

    session.add(CampRegistration(camp_id=camp.id, user_id=user_id))
    session.commit()


# --------------------------------------------------------------------------- #
# 创建和管理
# --------------------------------------------------------------------------- #


def test_admin_creates_camp(root_client):
    """管理员能创建集训。"""
    now = datetime.now(UTC)
    response = root_client.post(
        "/api/admin/camps",
        json={
            "name": "寒假集训",
            "description": "第一周",
            "start_at": (now + timedelta(hours=1)).isoformat(),
            "end_at": (now + timedelta(hours=25)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "寒假集训"


def test_admin_rejects_bad_time(root_client):
    """截止时间早于开始时间，拒。"""
    now = datetime.now(UTC)
    response = root_client.post(
        "/api/admin/camps",
        json={
            "name": "x",
            "start_at": (now + timedelta(hours=25)).isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 400


def test_member_cannot_create(member_client):
    """普通成员建不了。"""
    now = datetime.now(UTC)
    response = member_client.post(
        "/api/admin/camps",
        json={
            "name": "x",
            "start_at": now.isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 403


def test_admin_adds_and_removes_problem(root_client, db):
    """管理员加题、删题。"""
    camp = make_camp(db)
    response = root_client.post(
        f"/api/admin/camps/{camp.id}/problems",
        json={"platform": "codeforces", "key": "1234A"},
    )
    assert response.status_code == 201, response.text

    detail = root_client.get(f"/api/admin/camps/{camp.id}").json()
    assert len(detail["problems"]) == 1
    pid = detail["problems"][0]["id"]

    response = root_client.delete(f"/api/admin/camps/{camp.id}/problems/{pid}")
    assert response.status_code == 204
    assert root_client.get(f"/api/admin/camps/{camp.id}").json()["problems"] == []


# --------------------------------------------------------------------------- #
# 列表和详情
# --------------------------------------------------------------------------- #


def test_list_all_camps(client, db):
    """未登录也能看集训列表。"""
    make_camp(db, name="集训一")
    make_camp(db, name="集训二")

    rows = client.get("/api/camps").json()
    names = {r["name"] for r in rows}
    assert names == {"集训一", "集训二"}


def test_detail_hides_problems_until_registered(client, member_client, db):
    """未报名看不到题目和排行榜；未登录也一样。"""
    camp = make_camp(db)
    add_problem(db, camp, key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    make_account(db, uid, "codeforces", "cf_user", solved=["1A"])

    anonymous = client.get(f"/api/camps/{camp.id}").json()
    assert anonymous["problems"] == []
    assert anonymous["leaderboard"] == []

    outsider = member_client.get(f"/api/camps/{camp.id}").json()
    assert outsider["registered"] is False
    assert outsider["problems"] == []
    assert outsider["leaderboard"] == []
    # 但能知道自己能不能报
    assert outsider["can_register"] is True


def test_detail_shows_after_register(member_client, db):
    """报名后能看到题目和自己的排行。"""
    camp = make_camp(db)
    add_problem(db, camp, key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    make_account(db, uid, "codeforces", "cf_user", solved=["1A"])

    response = member_client.post(f"/api/camps/{camp.id}/register")
    assert response.status_code == 201, response.text

    detail = member_client.get(f"/api/camps/{camp.id}").json()
    assert detail["registered"] is True
    assert len(detail["problems"]) == 1
    assert len(detail["leaderboard"]) == 1
    entry = detail["leaderboard"][0]
    assert entry["solved_count"] == 1
    assert entry["per_problem"] == {"codeforces:1A": True}


# --------------------------------------------------------------------------- #
# 报名校验
# --------------------------------------------------------------------------- #


def test_register_requires_matching_account(member_client, db):
    """集训涉及的平台一个账号都没有，报不了。"""
    camp = make_camp(db)
    add_problem(db, camp, platform="codeforces", key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    # 只有洛谷账号，CF 的题报不了
    make_account(db, uid, "luogu", "1489036", verified=True)

    response = member_client.post(f"/api/camps/{camp.id}/register")
    assert response.status_code == 400
    assert "账号" in response.json()["detail"]


def test_register_needs_at_least_one(member_client, db):
    """多平台的集训，有其中一个账号就能报。"""
    camp = make_camp(db)
    add_problem(db, camp, platform="codeforces", key="1A")
    add_problem(db, camp, platform="luogu", key="P1001")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    make_account(db, uid, "luogu", "1489036", verified=True)

    response = member_client.post(f"/api/camps/{camp.id}/register")
    assert response.status_code == 201, response.text


def test_unverified_account_does_not_count(member_client, db):
    """没验证完的账号不算——「绑了」不等于「是你的」。"""
    camp = make_camp(db)
    add_problem(db, camp, platform="codeforces", key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    make_account(db, uid, "codeforces", "cf_user", verified=False)

    response = member_client.post(f"/api/camps/{camp.id}/register")
    assert response.status_code == 400


def test_register_after_deadline(member_client, db):
    """截止后报不了。"""
    camp = make_camp(db, end_offset=timedelta(hours=-1))
    add_problem(db, camp, key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    make_account(db, uid, "codeforces", "cf_user", verified=True)

    response = member_client.post(f"/api/camps/{camp.id}/register")
    assert response.status_code == 400
    assert "截止" in response.json()["detail"]


def test_register_twice_is_idempotent(member_client, db):
    """成功报名的重试返回相同状态，不新增报名。"""
    camp = make_camp(db)
    add_problem(db, camp, key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    make_account(db, uid, "codeforces", "cf_user", verified=True)

    assert member_client.post(f"/api/camps/{camp.id}/register").status_code == 201
    second = member_client.post(f"/api/camps/{camp.id}/register")
    assert second.status_code == 201
    assert member_client.get(f"/api/camps/{camp.id}").json()["registration_count"] == 1


def test_admin_adds_member_without_account(root_client, member_client, db):
    """管理员手动加人不受账号限制。"""
    camp = make_camp(db)
    add_problem(db, camp, key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    # 什么账号都没有

    response = root_client.post(
        f"/api/admin/camps/{camp.id}/members", json={"user_id": uid}
    )
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------- #
# 排行榜
# --------------------------------------------------------------------------- #


def test_leaderboard_only_counts_camp_problems(member_client, member2_client, db):
    """排行榜只算集训题，同一人的其他题不算。"""
    camp = make_camp(db)
    add_problem(db, camp, key="1A")
    add_problem(db, camp, key="1B")

    uid1 = user_id_of(db, MEMBER_STUDENT_ID)
    # 甲解了 1A 和 2C（2C 不是集训题）
    make_account(db, uid1, "codeforces", "cf_a", solved=["1A", "2C"])
    register_user(db, camp, uid1)

    uid2 = user_id_of(db, MEMBER2_STUDENT_ID)
    make_account(db, uid2, "codeforces", "cf_b", solved=["1A", "1B"])
    register_user(db, camp, uid2)

    board = camps.leaderboard(db, camp)
    assert len(board) == 2
    assert board[0][0].display_name == "队员乙"
    assert board[0][1] == 2
    assert board[1][1] == 1
    assert board[1][2] == {"codeforces:1A": True, "codeforces:1B": False}


def test_leaderboard_cross_platform(member_client, db):
    """不同平台的题各自算。"""
    camp = make_camp(db)
    add_problem(db, camp, platform="codeforces", key="1A")
    add_problem(db, camp, platform="luogu", key="P1001")

    uid = user_id_of(db, MEMBER_STUDENT_ID)
    make_account(db, uid, "codeforces", "cf", solved=["1A"])
    make_account(db, uid, "luogu", "1489036", solved=["P1001"])
    register_user(db, camp, uid)

    board = camps.leaderboard(db, camp)
    assert board[0][1] == 2
    assert board[0][2] == {"codeforces:1A": True, "luogu:P1001": True}


def test_leaderboard_empty_camp(member_client, db):
    """没题的集训排行榜为空。"""
    camp = make_camp(db)
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    register_user(db, camp, uid)

    assert camps.leaderboard(db, camp) == []


def test_leaderboard_solved_before_camp_counts(member_client, db):
    """「一百年前做的也算」——不看时间，截止时解了就算。"""
    camp = make_camp(db)
    add_problem(db, camp, key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    # 快照没存时间——solved_keys 只有题号集合，这正是用户要的口径。
    make_account(db, uid, "codeforces", "cf", solved=["1A"])
    register_user(db, camp, uid)

    board = camps.leaderboard(db, camp)
    assert board[0][1] == 1


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #


def test_export_json(root_client, member_client, db):
    """JSON 导出带表头和每人一行。"""
    camp = make_camp(db, name="导出测试")
    add_problem(db, camp, key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    make_account(db, uid, "codeforces", "cf", solved=["1A"])
    make_account(db, uid, "luogu", "1489036")
    register_user(db, camp, uid)

    response = root_client.get(f"/api/admin/camps/{camp.id}/export?format=json")
    assert response.status_code == 200
    data = json.loads(response.content)
    assert data["camp"]["name"] == "导出测试"
    assert "codeforces:1A" in data["headers"]
    row = data["rows"][0]
    # 学院 专业 班级 姓名 学号 洛谷号 过题数 每题
    assert row[3] == "队员甲"
    assert row[5] == "1489036"
    assert row[6] == "1"
    assert row[7] == "是"


def test_export_csv_has_bom(root_client, member_client, db):
    """CSV 带 BOM，Excel 打开不会乱码。"""
    camp = make_camp(db)
    add_problem(db, camp, key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    register_user(db, camp, uid)

    response = root_client.get(f"/api/admin/camps/{camp.id}/export?format=csv")
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert "队员甲" in response.text


def test_export_xlsx(root_client, member_client, db):
    """XLSX 导出。"""
    camp = make_camp(db)
    add_problem(db, camp, key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    register_user(db, camp, uid)

    response = root_client.get(f"/api/admin/camps/{camp.id}/export?format=xlsx")
    assert response.status_code == 200
    assert "spreadsheetml" in response.headers["content-type"]
    # 是真实的 zip（xlsx 就是 zip）
    assert response.content[:2] == b"PK"


def test_export_requires_admin(member_client, db):
    """普通成员不能导出。"""
    camp = make_camp(db)
    response = member_client.get(f"/api/admin/camps/{camp.id}/export?format=csv")
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# 同步加速
# --------------------------------------------------------------------------- #


def test_register_boosts_matching_accounts(member_client, db):
    """报名后，涉及平台的账号被标成「立刻要刷」+「集训内一小时一次」。"""
    camp = make_camp(db, end_offset=timedelta(hours=48))
    add_problem(db, camp, platform="luogu", key="P1001")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    lg = make_account(db, uid, "luogu", "1489036", verified=True)

    member_client.post(f"/api/camps/{camp.id}/register")

    db.refresh(lg)
    # 立刻要刷
    assert lg.solved_fetched_at is None
    # 加速窗口覆盖整个集训
    assert lg.sync_boost_until == camp.end_at


def test_register_does_not_touch_other_platforms(member_client, db):
    """CF 的集训不动洛谷账号。"""
    camp = make_camp(db)
    add_problem(db, camp, platform="codeforces", key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    cf = make_account(db, uid, "codeforces", "cf", verified=True)
    lg = make_account(db, uid, "luogu", "1489036", verified=True)

    member_client.post(f"/api/camps/{camp.id}/register")

    db.refresh(cf)
    db.refresh(lg)
    # CF 账号被加速，洛谷账号不动
    assert cf.solved_fetched_at is None
    assert cf.sync_boost_until == camp.end_at
    assert lg.sync_boost_until is None


def test_is_due_respects_boost(db):
    """加速窗口内，调度间隔被钳到一小时。"""
    from app.models import Account

    # 直接造账号，不走报名路径——这里测的是 is_due 本身。
    acc = Account(
        user_id=1,
        platform="luogu",
        identifier="1489036",
        verified_at=datetime.now(UTC),
        solved_fetched_at=datetime.now(UTC) - timedelta(hours=2),
        # 不活跃很久：正常间隔会很长
        last_active_at=datetime.now(UTC) - timedelta(days=10),
        sync_boost_until=datetime.now(UTC) + timedelta(hours=1),
    )
    now = datetime.now(UTC)
    # base=5min, cap=7day。不活跃 10 天的正常间隔远超一小时；
    # 但 boost 把它钳到一小时，两小时前刷过的现在到期。
    assert acc.is_due(now=now, base=300, cap=7 * 86400)

    # boost 过期就回落
    acc.sync_boost_until = now - timedelta(hours=1)
    assert not acc.is_due(now=now, base=300, cap=7 * 86400)


def test_ended_camp_marks_accounts_for_refresh(member_client, db):
    """截止后第一轮 sync 会把报名账号标成「立刻要刷」。

    排行榜算的是截止那一刻的已解决状态——过了截止时间才做的题不算，
    但截止前刷过的人等到自己下一次轮到才被抓，榜单就停在旧数据上。
    所以截止时要主动刷一轮。
    """
    from app import sync

    # 一份 10 分钟前刚截止的集训
    camp = make_camp(db, end_offset=timedelta(minutes=-10))
    add_problem(db, camp, platform="codeforces", key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    cf = make_account(db, uid, "codeforces", "cf", solved=["1A"])
    register_user(db, camp, uid)

    # 假装它两小时前刚刷过——正常调度下还不会轮到它
    cf.solved_fetched_at = datetime.now(UTC) - timedelta(hours=2)
    cf.last_active_at = datetime.now(UTC) - timedelta(days=10)
    db.add(cf)
    db.commit()

    # 跑一轮 sync 的开头钩子（fetch_solved 会真的发请求，把 platforms 限成
    # 一个不存在的平台，钩子照跑但一个账号都不会真抓）
    sync.run_sync(platforms=["nonexistent"])

    db.refresh(cf)
    # 截止钩子把它标成了立刻要刷——但下一轮就会真正抓到
    # 注意：solved_fetched_at=None 之后钩子这轮就抓不到它了（选择列表在
    # 钩子之前生成），下一轮才轮到。这是设计如此。
    assert cf.solved_fetched_at is None


def test_running_camp_does_not_trigger_refresh(member_client, db):
    """没截止的集训不触发刷新。"""
    from app import sync

    camp = make_camp(db)  # 默认还差 23 小时截止
    add_problem(db, camp, platform="codeforces", key="1A")
    uid = user_id_of(db, MEMBER_STUDENT_ID)
    cf = make_account(db, uid, "codeforces", "cf", solved=["1A"])
    register_user(db, camp, uid)

    past = datetime.now(UTC) - timedelta(hours=2)
    cf.solved_fetched_at = past
    db.add(cf)
    db.commit()

    sync.run_sync(platforms=["nonexistent"])

    db.refresh(cf)
    # 没被动过
    assert cf.solved_fetched_at == past
