"""每日练习量统计的测试。

重点覆盖两件容易错的事：

* 一个人在同一平台绑了多个账号时，两个账号的题数要相加而不是互相覆盖。
* 重复同步不能重复计数。CF/AtCoder 的提交带时间戳，按日覆盖写；洛谷靠
  快照差分，第一次只建基线不产生增量。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.db import get_engine
from app.models import Account, DailyProgress, Platform, SolvedSnapshot, User, utcnow
from app.platforms import SolvedSet
from app.sync import CST, _apply_diff, _apply_timestamped, sync_account


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'sync.db'}")
    monkeypatch.setenv("SGU_ACW_SCRYPT_N", "1024")

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()

    from app.db import init_db

    init_db()
    yield
    db_module.reset_engine()
    get_settings.cache_clear()


def _make_user(db: Session, student_id: str, name: str) -> User:
    user = User(
        student_id=student_id,
        password_hash="x",
        display_name=name,
        enroll_year=2024,
        level=1,
        college_code="25",
        major_code="02",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_account(
    db: Session, user: User, identifier: str, platform: str = Platform.CODEFORCES
) -> Account:
    account = Account(
        user_id=user.id,
        platform=platform,
        identifier=identifier,
        verified_at=utcnow(),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _epoch(days_ago: int, hour: int = 12) -> int:
    """距今 ``days_ago`` 个练习日、北京时间 ``hour`` 点的 epoch 秒。

    以练习日为基准而不是自然日，这样在凌晨 0 到 3 点跑测试时，
    ``_epoch(0)`` 和 ``_day(0)`` 仍然指向同一天。
    """
    from datetime import date

    from app.sync import current_practice_day

    day = date.fromisoformat(current_practice_day()) - timedelta(days=days_ago)
    moment = datetime(day.year, day.month, day.day, hour, tzinfo=CST)
    return int(moment.timestamp())


def _day(days_ago: int) -> str:
    """距今 ``days_ago`` 个练习日的日期。"""
    from datetime import date

    from app.sync import current_practice_day

    today = date.fromisoformat(current_practice_day())
    return (today - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _progress(db: Session, account: Account, date: str) -> int:
    row = db.exec(
        select(DailyProgress).where(
            DailyProgress.account_id == account.id, DailyProgress.date == date
        )
    ).first()
    return row.solved_count if row else 0


def test_timestamped_writes_per_day(env):
    """有时间的平台按天归档。"""
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "alpha")

        solved = SolvedSet(
            platform=Platform.CODEFORCES,
            identifier="alpha",
            problems={"a": _epoch(1), "b": _epoch(1), "c": _epoch(2)},
        )
        _apply_timestamped(db, account, solved)
        db.commit()

        assert _progress(db, account, _day(1)) == 2
        assert _progress(db, account, _day(2)) == 1


def test_timestamped_is_idempotent(env):
    """同一份数据同步两次，计数不变。"""
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "alpha")
        solved = SolvedSet(
            platform=Platform.CODEFORCES,
            identifier="alpha",
            problems={"a": _epoch(1), "b": _epoch(1)},
        )

        _apply_timestamped(db, account, solved)
        db.commit()
        first = _progress(db, account, _day(1))

        _apply_timestamped(db, account, solved)
        db.commit()
        assert _progress(db, account, _day(1)) == first == 2


def test_two_accounts_same_user_do_not_overwrite(env):
    """同一个人两个账号，各记各的，互不覆盖。

    这是按账号而非按人建键的原因：按人建键的话，后同步的账号会把先前的
    覆盖掉，总量凭空少一半。
    """
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        main = _make_account(db, user, "main")
        alt = _make_account(db, user, "alt")

        _apply_timestamped(
            db, main, SolvedSet(Platform.CODEFORCES, "main", {"a": _epoch(1), "b": _epoch(1)})
        )
        _apply_timestamped(db, alt, SolvedSet(Platform.CODEFORCES, "alt", {"c": _epoch(1)}))
        db.commit()

        assert _progress(db, main, _day(1)) == 2
        assert _progress(db, alt, _day(1)) == 1

        # 按人汇总时应当是 3。
        rows = db.exec(select(DailyProgress).where(DailyProgress.date == _day(1))).all()
        assert sum(r.solved_count for r in rows) == 3


def test_diff_first_sync_creates_baseline_only(env):
    """洛谷首次同步只建基线，不产生增量。

    否则会把一个人几年的积累全算到今天。
    """
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "1425874", platform=Platform.LUOGU)

        added = _apply_diff(
            db,
            account,
            SolvedSet(Platform.LUOGU, "1425874", {"P1001": 0, "P1002": 0, "P1003": 0}),
        )
        db.commit()

        assert added == 0
        rows = db.exec(select(DailyProgress).where(DailyProgress.account_id == account.id)).all()
        assert rows == []

        snapshot = db.exec(
            select(SolvedSnapshot).where(SolvedSnapshot.account_id == account.id)
        ).one()
        assert snapshot.problem_count == 3


def test_diff_second_sync_counts_new_problems(env):
    """第二次同步多出来的题记为增量。"""
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "1425874", platform=Platform.LUOGU)

        _apply_diff(db, account, SolvedSet(Platform.LUOGU, "1425874", {"P1": 0, "P2": 0}))
        db.commit()

        added = _apply_diff(
            db, account, SolvedSet(Platform.LUOGU, "1425874", {"P1": 0, "P2": 0, "P3": 0, "P4": 0})
        )
        db.commit()

        assert added == 2
        total = sum(
            r.solved_count
            for r in db.exec(
                select(DailyProgress).where(DailyProgress.account_id == account.id)
            ).all()
        )
        assert total == 2


def test_diff_with_no_change_adds_nothing(env):
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "1425874", platform=Platform.LUOGU)

        _apply_diff(db, account, SolvedSet(Platform.LUOGU, "1425874", {"P1": 0}))
        db.commit()
        added = _apply_diff(db, account, SolvedSet(Platform.LUOGU, "1425874", {"P1": 0}))
        db.commit()

        assert added == 0


def test_sync_account_reports_fetch_failure(env, monkeypatch):
    """抓取失败时返回失败而不是抛异常，让同步继续跑完其他账号。"""
    from app.platforms import FetchError

    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "ghost")

        def boom(platform, identifier):
            raise FetchError("平台上没有这个用户", not_found=True)

        monkeypatch.setattr("app.sync.fetch_solved", boom)
        ok, detail, added = sync_account(db, account)

        assert ok is False
        assert "没有这个用户" in detail
        assert added == 0


def test_day_boundary_uses_east_eight(env):
    """按东八区切日，且凌晨 4 点才换天。

    北京时间今天 01:00 属于**昨天**那个练习日：比赛打到半夜，凌晨交的题
    在感觉上还是昨天那一场。
    """
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "alpha")

        solved = SolvedSet(Platform.CODEFORCES, "alpha", {"a": _epoch(0, hour=1)})
        _apply_timestamped(db, account, solved)
        db.commit()

        # 归到昨天，不是今天。
        assert _progress(db, account, _day(1)) == 1
        assert _progress(db, account, _day(0)) == 0


def test_day_boundary_at_four_am(env):
    """4 点前后分属不同的练习日。"""
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "alpha")

        # 今天 03:30 仍是昨天，04:30 就是今天。
        _apply_timestamped(
            db, account, SolvedSet(Platform.CODEFORCES, "alpha", {"a": _epoch(0, hour=3)})
        )
        _apply_timestamped(
            db, account, SolvedSet(Platform.CODEFORCES, "alpha", {"b": _epoch(0, hour=5)})
        )
        db.commit()

        assert _progress(db, account, _day(1)) == 1  # 03:30 → 昨天
        assert _progress(db, account, _day(0)) == 1  # 05:00 → 今天


def test_practice_day_helper():
    """练习日的定义：东八区减 4 小时取日期。"""
    from datetime import datetime

    from app.sync import CST, practice_day_of

    cases = [
        (datetime(2026, 10, 1, 0, 30, tzinfo=CST), "2026-09-30"),
        (datetime(2026, 10, 1, 3, 59, tzinfo=CST), "2026-09-30"),
        (datetime(2026, 10, 1, 4, 0, tzinfo=CST), "2026-10-01"),
        (datetime(2026, 10, 1, 23, 30, tzinfo=CST), "2026-10-01"),
    ]
    for moment, expected in cases:
        assert practice_day_of(moment) == expected, moment


# --------------------------------------------------------------------------- #
# 牛客
# --------------------------------------------------------------------------- #


def test_nowcoder_row_parsing():
    """牛客练习页的表格解析。

    这页是服务端直出的 HTML，字段位置固定：运行ID、题目、判题结果、得分、
    运行时间、内存、代码长度、语言、提交时间。解析错了会静默少算题数，
    所以拿一段真实结构的片段钉住。
    """
    from app.platforms import _nowcoder_rows, _parse_nowcoder_time

    page = """
    <table><tbody>
    <tr><td>84566970</td><td><a href="/acm/problem/318505">恋爱模拟器</a></td>
        <td>答案正确</td><td>100</td><td>112</td><td>26168</td><td>319</td>
        <td>PyPy3</td><td>2026-08-19 23:47:35</td></tr>
    <tr><td>84566959</td><td><a href="/acm/problem/318505">恋爱模拟器</a></td>
        <td>答案错误</td><td>0</td><td>121</td><td>25532</td><td>262</td>
        <td>PyPy3</td><td>2026-08-19 23:44:01</td></tr>
    <tr><th>表头不是数据行</th></tr>
    </tbody></table>
    """
    rows = _nowcoder_rows(page)
    assert len(rows) == 2
    assert rows[0].run_id == "84566970"
    assert rows[0].problem_id == "318505", "题目 id 要从链接里取，剥标签会丢掉"
    assert rows[0].title == "恋爱模拟器"
    assert rows[0].verdict == "答案正确"
    assert rows[0].submitted_at == "2026-08-19 23:47:35"
    assert rows[1].verdict == "答案错误"

    # 页面上的时间是北京时间，不带时区标记。
    epoch = _parse_nowcoder_time("2026-08-19 23:47:35")
    assert epoch is not None
    assert _day_of_epoch(epoch) == "2026-08-19"


def _day_of_epoch(epoch: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).astimezone(CST).strftime("%Y-%m-%d")


def test_nowcoder_counts_both_sources(env):
    """牛客要同时抓竞赛站和主站。

    两边几乎不重合——实测某用户主站 88 题、竞赛站 192 题，交集只有 1 题。
    只抓一边会漏掉一大块。
    """
    from app.platforms import fetch_solved_nowcoder

    contest_page = """
    <table><tbody>
    <tr><td>1</td><td><a href="/acm/problem/318505">题目甲</a></td>
        <td>答案正确</td><td>100</td><td>1</td>
        <td>1</td><td>1</td><td>PyPy3</td><td>2026-08-20 10:00:00</td></tr>
    </tbody></table>
    """

    class FakeResponse:
        status_code = 200
        text = contest_page

        @staticmethod
        def json():
            return {
                "success": True,
                "code": 0,
                "data": {
                    "total": 1,
                    "records": [
                        {
                            "problem": {"questionUuid": "uuid-abc", "title": "多组输入"},
                            "accept": True,
                            "submission": {"createdDate": 1790932328000},
                        }
                    ],
                },
            }

    class FakeClient:
        def get(self, *args, **kwargs):
            return FakeResponse()

        def post(self, *args, **kwargs):
            return FakeResponse()

    solved = fetch_solved_nowcoder(FakeClient(), "825377486")
    assert set(solved.problems) == {"nowcoder:contest:318505", "nowcoder:main:uuid-abc"}


def test_nowcoder_contest_only_counts_accepted(env):
    """没通过的提交不算过题。"""
    from app.platforms import fetch_solved_nowcoder

    page = """
    <table><tbody>
    <tr><td>1</td><td><a href="/acm/problem/318505">题目甲</a></td>
        <td>答案正确</td><td>100</td><td>1</td>
        <td>1</td><td>1</td><td>PyPy3</td><td>2026-08-20 10:00:00</td></tr>
    <tr><td>2</td><td><a href="/acm/problem/318506">题目乙</a></td>
        <td>运行超时</td><td>100</td><td>1</td>
        <td>1</td><td>1</td><td>PyPy3</td><td>2026-08-20 10:00:00</td></tr>
    </tbody></table>
    """

    class FakeResponse:
        status_code = 200
        text = page

        @staticmethod
        def json():
            return {"success": True, "code": 0, "data": {"total": 0, "records": []}}

    class FakeClient:
        def get(self, *args, **kwargs):
            return FakeResponse()

        def post(self, *args, **kwargs):
            return FakeResponse()

    solved = fetch_solved_nowcoder(FakeClient(), "825377486")
    assert set(solved.problems) == {"nowcoder:contest:318505"}


def test_nowcoder_dedupes_by_earliest(env):
    """同一题多次通过时只记最早一次。"""
    from app.platforms import fetch_solved_nowcoder

    page = """
    <table><tbody>
    <tr><td>1</td><td><a href="/acm/problem/318505">题目甲</a></td>
        <td>答案正确</td><td>100</td><td>1</td>
        <td>1</td><td>1</td><td>PyPy3</td><td>2026-08-20 10:00:00</td></tr>
    <tr><td>2</td><td><a href="/acm/problem/318505">题目甲</a></td>
        <td>答案正确</td><td>100</td><td>1</td>
        <td>1</td><td>1</td><td>PyPy3</td><td>2026-08-19 10:00:00</td></tr>
    </tbody></table>
    """

    class FakeResponse:
        status_code = 200
        text = page

        @staticmethod
        def json():
            return {"success": True, "code": 0, "data": {"total": 0, "records": []}}

    class FakeClient:
        def get(self, *args, **kwargs):
            return FakeResponse()

        def post(self, *args, **kwargs):
            return FakeResponse()

    solved = fetch_solved_nowcoder(FakeClient(), "825377486")
    assert _day_of_epoch(solved.problems["nowcoder:contest:318505"]) == "2026-08-19"


def test_nowcoder_main_dedupes_by_earliest(env):
    """主站同一题多次通过也只记最早一次。"""
    from app.platforms import fetch_solved_nowcoder

    class FakeResponse:
        status_code = 200
        text = "<table><tbody></tbody></table>"

        @staticmethod
        def json():
            return {
                "success": True,
                "code": 0,
                "data": {
                    "total": 2,
                    "records": [
                        {
                            "problem": {"questionUuid": "uuid-xyz", "title": "加法入门"},
                            "accept": True,
                            "submission": {"createdDate": 1790932328000},
                        },
                        {
                            "problem": {"questionUuid": "uuid-xyz", "title": "加法入门"},
                            "accept": True,
                            "submission": {"createdDate": 1780000000000},
                        },
                    ],
                },
            }

    class FakeClient:
        def get(self, *args, **kwargs):
            return FakeResponse()

        def post(self, *args, **kwargs):
            return FakeResponse()

    solved = fetch_solved_nowcoder(FakeClient(), "825377486")
    assert _day_of_epoch(solved.problems["nowcoder:main:uuid-xyz"]) == "2026-05-29"


def test_nowcoder_survives_one_source_failing(env):
    """一边挂了不该让整个同步失败——另一边往往还有数据。"""
    from app.platforms import fetch_solved_nowcoder

    contest_page = """
    <table><tbody>
    <tr><td>1</td><td><a href="/acm/problem/318505">题目甲</a></td>
        <td>答案正确</td><td>100</td><td>1</td>
        <td>1</td><td>1</td><td>PyPy3</td><td>2026-08-20 10:00:00</td></tr>
    </tbody></table>
    """

    class OkResponse:
        status_code = 200
        text = contest_page

    class BrokenResponse:
        status_code = 500
        text = ""

        @staticmethod
        def json():
            return {}

    class FakeClient:
        def get(self, *args, **kwargs):
            return OkResponse()

        def post(self, *args, **kwargs):
            return BrokenResponse()

    solved = fetch_solved_nowcoder(FakeClient(), "825377486")
    assert set(solved.problems) == {"nowcoder:contest:318505"}


def test_nowcoder_successfully_reads_empty_solved_set(env):
    """两路数据都正常读取但没有 AC 时，应成功返回空集合。"""
    from app.platforms import fetch_solved_nowcoder

    contest_page = """
    <table><tbody>
    <tr><td>1</td><td><a href="/acm/problem/318505">题目甲</a></td>
        <td>运行超时</td><td>0</td><td>1</td>
        <td>1</td><td>1</td><td>PyPy3</td><td>2026-08-20 10:00:00</td></tr>
    </tbody></table>
    """

    class FakeResponse:
        status_code = 200
        text = contest_page

        @staticmethod
        def json():
            return {
                "success": True,
                "code": 0,
                "data": {
                    "total": 1,
                    "records": [
                        {
                            "problem": {"questionUuid": "uuid-abc", "title": "多组输入"},
                            "accept": False,
                            "submission": {"createdDate": 1790932328000},
                        }
                    ],
                },
            }

    class FakeClient:
        def get(self, *args, **kwargs):
            return FakeResponse()

        def post(self, *args, **kwargs):
            return FakeResponse()

    solved = fetch_solved_nowcoder(FakeClient(), "825377486")
    assert solved.problems == {}


def test_nowcoder_empty_result_with_source_error_still_fails(env):
    """一路不可用时，另一路的空数据不能证明账号确实没有 AC。"""
    from app.platforms import FetchError, fetch_solved_nowcoder

    class EmptyResponse:
        status_code = 200
        text = "<table><tbody></tbody></table>"

        @staticmethod
        def json():
            return {"success": True, "code": 0, "data": {"records": []}}

    class BrokenResponse:
        status_code = 503
        text = ""

    class FakeClient:
        def get(self, *args, **kwargs):
            return EmptyResponse()

        def post(self, *args, **kwargs):
            return BrokenResponse()

    with pytest.raises(FetchError, match="牛客主站返回 503"):
        fetch_solved_nowcoder(FakeClient(), "825377486")


# --------------------------------------------------------------------------- #
# 自适应调度
# --------------------------------------------------------------------------- #


def _interval(account, now):
    from app.config import get_settings

    s = get_settings()
    return account.sync_interval_seconds(
        now=now,
        base=s.sync_interval_base_minutes * 60,
        cap=s.sync_interval_cap_minutes * 60,
    )


def _kwargs():
    from app.config import get_settings

    s = get_settings()
    return {
        "base": s.sync_interval_base_minutes * 60,
        "cap": s.sync_interval_cap_minutes * 60,
    }


def test_new_account_starts_at_curve_base(env):
    """刚注册的账号落在曲线起点。

    没有单独一个「新人档」：从没拓到过新题时，不活跃天数从注册时间算，
    自然就是 0 天，取到最小间隔。
    """
    from app.config import get_settings

    now = utcnow()
    # 刚注册几分钟：不活跃天数接近 0，2^0 = 1，取到起点。
    account = Account(
        user_id=1,
        platform=Platform.CODEFORCES,
        identifier="x",
        created_at=now - timedelta(minutes=1),
    )
    assert _interval(account, now) == get_settings().sync_interval_base_minutes * 60


def test_interval_doubles_each_idle_day(env):
    """每多一天不活跃，间隔翻一倍。

    这是曲线的核心性质，比固定档位的跳变更贴近实际：活跃度是连续变化的，
    不该在某个时刻从一小时直接跳到六小时。
    """
    from app.config import get_settings

    base = get_settings().sync_interval_base_minutes * 60
    now = utcnow()

    for days, factor in [(0, 1), (1, 2), (2, 4), (3, 8), (4, 16)]:
        account = Account(
            user_id=1,
            platform=Platform.CODEFORCES,
            identifier="x",
            created_at=now - timedelta(days=60),
        )
        account.last_active_at = now - timedelta(days=days)
        assert _interval(account, now) == base * factor, f"{days} 天"


def test_interval_is_capped(env):
    """间隔封顶，不会无限增长。

    封顶之外再慢就没意义了——没人会等八天看自己的数字。真到了谷值还想
    立刻更新，成员可以手动刷新。
    """
    from app.config import get_settings

    cap = get_settings().sync_interval_cap_minutes * 60
    now = utcnow()

    for days in [10, 14, 30, 365, 3650]:
        account = Account(
            user_id=1,
            platform=Platform.CODEFORCES,
            identifier="x",
            created_at=now - timedelta(days=60),
        )
        account.last_active_at = now - timedelta(days=days)
        assert _interval(account, now) == cap, f"{days} 天"


def test_never_active_account_slows_down_with_age(env):
    """注册久了却一直没做过题，也会沿曲线放慢。

    关键点：不活跃天数从注册时间算起，而不是从「上次同步」算。否则一个
    不做题的人会因为反复被查看而一直停在最小间隔。
    """
    from app.config import get_settings

    s = get_settings()
    now = utcnow()

    brand_new = Account(
        user_id=1,
        platform=Platform.CODEFORCES,
        identifier="a",
        created_at=now - timedelta(minutes=1),
    )
    # 注册一天：2^1 = 2，间隔翻一倍。
    one_day = Account(
        user_id=1,
        platform=Platform.CODEFORCES,
        identifier="b",
        created_at=now - timedelta(days=1),
    )
    old = Account(
        user_id=1,
        platform=Platform.CODEFORCES,
        identifier="c",
        created_at=now - timedelta(days=30),
    )

    assert _interval(brand_new, now) == s.sync_interval_base_minutes * 60
    assert _interval(one_day, now) == s.sync_interval_base_minutes * 2 * 60
    # 30 天远超封顶所需，直接到顶。
    assert _interval(old, now) == s.sync_interval_cap_minutes * 60


def test_is_due_respects_interval(env):
    from app.config import get_settings

    s = get_settings()
    now = utcnow()
    account = Account(
        user_id=1,
        platform=Platform.CODEFORCES,
        identifier="x",
        created_at=now - timedelta(days=60),
    )
    # 昨天做过题 → 间隔是起点的一倍。
    account.last_active_at = now - timedelta(days=1)
    interval_min = s.sync_interval_base_minutes * 2

    # 从没抓过：立刻该抓。
    account.solved_fetched_at = None
    assert account.is_due(now=now, **_kwargs())

    # 刚抓过：还不到时候。
    account.solved_fetched_at = now - timedelta(minutes=1)
    assert not account.is_due(now=now, **_kwargs())

    # 超过当前间隔：该抓了。
    account.solved_fetched_at = now - timedelta(minutes=interval_min + 1)
    assert account.is_due(now=now, **_kwargs())


def test_timestamped_returns_real_delta_not_total(env):
    """有时间的平台要返回真正新增数，不是窗口内总数。

    返回总数的话，一个不做题的人每次同步都显得有新题，自适应调度就失效了。
    """
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "alpha")

        first = _apply_timestamped(
            db,
            account,
            SolvedSet(Platform.CODEFORCES, "alpha", {"a": _epoch(1), "b": _epoch(1)}),
        )
        db.commit()
        assert first == 2

        # 同一份数据再来一次：没有新增。
        again = _apply_timestamped(
            db,
            account,
            SolvedSet(Platform.CODEFORCES, "alpha", {"a": _epoch(1), "b": _epoch(1)}),
        )
        db.commit()
        assert again == 0

        # 多了一题：新增 1。
        more = _apply_timestamped(
            db,
            account,
            SolvedSet(
                Platform.CODEFORCES, "alpha", {"a": _epoch(1), "b": _epoch(1), "c": _epoch(1)}
            ),
        )
        db.commit()
        assert more == 1


# --------------------------------------------------------------------------- #
# rating 榜
# --------------------------------------------------------------------------- #


def test_rating_board_is_per_account_not_per_person(env):
    """rating 榜以账号为单位。

    一个人可以有主号和小号，两个号各有各的 rating，合并成一个数没有意义。
    这是与练习量榜最大的不同——后者按人合并，前者不合并。
    """
    from app.routers.practice import rating_board

    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        main = _make_account(db, user, "main")
        alt = _make_account(db, user, "alt")
        main.rating = 1667
        alt.rating = 1599
        db.add(main)
        db.add(alt)
        db.commit()

        board = rating_board(_current=user, db=db, platform=Platform.CODEFORCES)

        # 两行都是同一个人。
        assert len(board.entries) == 2
        assert {e.user_id for e in board.entries} == {user.id}
        assert [e.identifier for e in board.entries] == ["main", "alt"]
        assert [e.rating for e in board.entries] == [1667, 1599]


def test_rating_board_skips_accounts_without_rating(env):
    """还没抓到 rating 的账号不列出。

    列出来的话会占着位置显示一个空值，看起来像是数据坏了。
    """
    from app.routers.practice import rating_board

    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        ready = _make_account(db, user, "ready")
        ready.rating = 1500
        db.add(ready)
        _make_account(db, user, "pending")  # rating 仍为 None
        db.commit()

        board = rating_board(_current=user, db=db, platform=Platform.CODEFORCES)
        assert [e.identifier for e in board.entries] == ["ready"]


def test_rating_board_only_shows_verified_accounts(env):
    """未验证的账号不上榜。"""
    from app.routers.practice import rating_board

    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "unverified")
        account.verified_at = None
        account.rating = 1500
        db.add(account)
        db.commit()

        board = rating_board(_current=user, db=db, platform=Platform.CODEFORCES)
        assert board.entries == []


def test_rating_board_sorted_desc(env):
    from app.routers.practice import rating_board

    with Session(get_engine()) as db:
        low = _make_user(db, "24125023001", "低")
        high = _make_user(db, "24125023002", "高")
        a = _make_account(db, low, "low")
        a.rating = 1200
        b = _make_account(db, high, "high")
        b.rating = 2000
        db.add(a)
        db.add(b)
        db.commit()

        board = rating_board(_current=low, db=db, platform=Platform.CODEFORCES)
        assert [e.identifier for e in board.entries] == ["high", "low"]
        assert board.entries[0].rank == 1


def test_failures_back_off(env):
    """连续失败要退避。

    否则一个已经注销的平台账号会每五分钟撞一次墙，白耗请求，日志里还全是
    同一行错误。
    """
    from app.config import get_settings

    s = get_settings()
    base = s.sync_interval_base_minutes * 60
    cap = s.sync_interval_cap_minutes * 60
    now = utcnow()

    def due_after(failures, minutes_ago):
        account = Account(
            user_id=1,
            platform=Platform.CODEFORCES,
            identifier="x",
            created_at=now - timedelta(days=1),
        )
        account.last_active_at = now
        account.consecutive_failures = failures
        account.solved_fetched_at = now - timedelta(minutes=minutes_ago)
        return account.is_due(now=now, base=base, cap=cap)

    # 正常账号（无失败）15 分钟就该抓了。
    assert due_after(0, 16)
    assert not due_after(0, 10)

    # 失败一次：间隔翻倍到 30 分钟，16 分钟时还不该抓。
    assert not due_after(1, 16)
    assert due_after(1, 31)

    # 失败两次：1 小时。
    assert not due_after(2, 50)
    assert due_after(2, 61)


def test_failure_backoff_is_capped(env):
    """退避有上限，不会无限增长。"""
    from app.config import get_settings

    s = get_settings()
    now = utcnow()
    account = Account(
        user_id=1,
        platform=Platform.CODEFORCES,
        identifier="x",
        created_at=now - timedelta(days=1),
    )
    account.last_active_at = now
    account.consecutive_failures = 50
    account.solved_fetched_at = now - timedelta(days=6)

    # 6 天还没到封顶的 7 天。
    assert not account.is_due(
        now=now,
        base=s.sync_interval_base_minutes * 60,
        cap=s.sync_interval_cap_minutes * 60,
    )
    account.solved_fetched_at = now - timedelta(days=8)
    assert account.is_due(
        now=now,
        base=s.sync_interval_base_minutes * 60,
        cap=s.sync_interval_cap_minutes * 60,
    )


def test_success_clears_failure_counter(env, monkeypatch):
    """一次成功就把失败计数清零。"""
    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "alpha")
        account.consecutive_failures = 5
        db.add(account)
        db.commit()

        monkeypatch.setattr(
            "app.sync.fetch_solved",
            lambda platform, identifier: SolvedSet(Platform.CODEFORCES, "alpha", {"a": _epoch(1)}),
        )
        ok, _, _ = sync_account(db, account, force=True)
        db.commit()

        assert ok
        assert account.consecutive_failures == 0


def test_scheduling_is_per_account_not_per_person(env):
    """调度必须细分到账号，不能按人。

    同一个人可能有活跃的主号和一个早就不用的僵尸小号。如果调度挂在「人」
    上，这个人的活跃会带着僵尸号一起高频抓取——一个 45 天没动过的号会
    一天白抓 96 次，一年三万多次无效请求。

    所以 is_due / sync_interval_seconds 挂在 Account 上，不是 User 上。
    """
    from app.config import get_settings

    s = get_settings()
    base = s.sync_interval_base_minutes * 60
    cap = s.sync_interval_cap_minutes * 60
    now = utcnow()

    def make(identifier, active_days_ago):
        account = Account(
            user_id=1,
            platform=Platform.CODEFORCES,
            identifier=identifier,
            created_at=now - timedelta(days=90),
        )
        if active_days_ago is not None:
            account.last_active_at = now - timedelta(days=active_days_ago)
        return account

    # 同一个 user_id，两个号。
    active = make("main", 0)
    zombie = make("alt", 45)

    assert active.sync_interval_seconds(now=now, base=base, cap=cap) == base
    assert zombie.sync_interval_seconds(now=now, base=base, cap=cap) == cap

    # 两个号刚抓过同样的时间：活跃的该抓了，僵尸的还早。
    for account in (active, zombie):
        account.solved_fetched_at = now - timedelta(minutes=20)

    assert active.is_due(now=now, base=base, cap=cap)
    assert not zombie.is_due(now=now, base=base, cap=cap)


def test_zombie_account_gets_few_requests(env):
    """僵尸号的抓取频率要真的低下来。

    按天算一下请求量，确保量级对了——这是「细分到号」的实际收益。
    """
    from app.config import get_settings

    s = get_settings()
    now = utcnow()
    zombie = Account(
        user_id=1,
        platform=Platform.CODEFORCES,
        identifier="alt",
        created_at=now - timedelta(days=200),
    )
    zombie.last_active_at = now - timedelta(days=100)

    interval = zombie.sync_interval_seconds(
        now=now,
        base=s.sync_interval_base_minutes * 60,
        cap=s.sync_interval_cap_minutes * 60,
    )
    per_day = 86400 / interval

    # 封顶 7 天 → 一天不到 1 次。
    assert per_day < 1
    # 对比：如果跟着活跃的主号走（15 分钟），一天是 96 次。
    assert per_day < 96 / 10


# --------------------------------------------------------------------------- #
# Codeforces 的签名字段
# --------------------------------------------------------------------------- #


def test_codeforces_signature_reads_multiple_fields():
    """CF 的验证码可以填在名字、姓氏、城市任意一处。

    不能用 organization：那是个选择器，会要求从已有组织里选或者新建一个，
    填不进任意文本。而名字、姓氏、城市都是自由文本。

    读多个字段是因为它们各有各的用途：有人拿 lastName 放真姓氏（xie），
    有人用 city（Shantou）。全算上就不会强迫成员改掉真实信息。
    """
    from app.platforms import fetch_codeforces

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "status": "OK",
                "result": [
                    {
                        "handle": "tester",
                        "firstName": "Gennady",
                        "lastName": "Korotkevich",
                        "city": "Gomel",
                        "organization": "ITMO University",
                    }
                ],
            }

    class FakeClient:
        def get(self, *args, **kwargs):
            return FakeResponse()

    profile = fetch_codeforces(FakeClient(), "tester")
    for expected in ["Gennady", "Korotkevich", "Gomel", "ITMO University"]:
        assert expected in profile.signature


def test_codeforces_signature_skips_missing_fields():
    """没填的字段不该在签名里留下空行。"""
    from app.platforms import fetch_codeforces

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "status": "OK",
                "result": [{"handle": "tester", "lastName": "SGU-ABCD"}],
            }

    class FakeClient:
        def get(self, *args, **kwargs):
            return FakeResponse()

    profile = fetch_codeforces(FakeClient(), "tester")
    assert profile.signature == "SGU-ABCD"
    assert "\n\n" not in profile.signature


def test_codeforces_missing_handle_is_not_found():
    from app.platforms import FetchError, fetch_codeforces

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "FAILED", "comment": "handle not found"}

    class FakeClient:
        def get(self, *args, **kwargs):
            return FakeResponse()

    with pytest.raises(FetchError) as exc:
        fetch_codeforces(FakeClient(), "nobody")
    assert exc.value.not_found


# --------------------------------------------------------------------------- #
# 榜单的可复用入口
# --------------------------------------------------------------------------- #


def test_build_practice_rank_callable_directly(env):
    """排序逻辑必须能直接调用，不能只有 FastAPI 能调。

    dashboard 接口复用了这套逻辑。如果把它写成带 ``Query(default=...)`` 的
    路由函数，直接调用时那几个参数拿到的是 ``Query`` 对象而不是默认值，
    后面一句 ``q.strip()`` 就会炸成 500。线上这么踩过。

    所以排序逻辑在 build_practice_rank（普通函数）里，路由只是薄薄一层。
    """
    from app.routers.practice import build_practice_rank

    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "alpha")
        _apply_timestamped(
            db,
            account,
            SolvedSet(Platform.CODEFORCES, "alpha", {"a": _epoch(1), "b": _epoch(2)}),
        )
        db.commit()

        # 不传任何筛选参数，也不经过 FastAPI。
        result = build_practice_rank(db, days=30)
        assert len(result.entries) == 1
        assert result.entries[0].display_name == "甲"
        assert result.entries[0].total == 2


def test_build_practice_rank_filters(env):
    """筛选在普通函数里也要生效。"""
    from app.routers.practice import build_practice_rank

    with Session(get_engine()) as db:
        a = _make_user(db, "24125023001", "甲")
        b = _make_user(db, "25125023002", "乙")
        for user in (a, b):
            account = _make_account(db, user, f"acc{user.id}")
            _apply_timestamped(
                db,
                account,
                SolvedSet(Platform.CODEFORCES, f"acc{user.id}", {"p": _epoch(1)}),
            )
        db.commit()

        # 按姓名搜。
        assert len(build_practice_rank(db, days=30, q="甲").entries) == 1
        # 按学号搜。
        assert len(build_practice_rank(db, days=30, q="2512").entries) == 1
        # 按年级筛。_make_user 固定造 2024 级，所以两人都命中。
        assert len(build_practice_rank(db, days=30, enroll_year=2024).entries) == 2
        assert len(build_practice_rank(db, days=30, enroll_year=2025).entries) == 0
        # 按学院筛（两人都是 25 学院）。
        assert len(build_practice_rank(db, days=30, college="信息工程学院").entries) == 2
        assert len(build_practice_rank(db, days=30, college="商学院").entries) == 0


def test_rank_entry_carries_college_names(env):
    """榜单每行要带上学院名与专业名。"""
    from app.routers.practice import build_practice_rank

    with Session(get_engine()) as db:
        user = _make_user(db, "24125023001", "甲")
        account = _make_account(db, user, "alpha")
        _apply_timestamped(db, account, SolvedSet(Platform.CODEFORCES, "alpha", {"a": _epoch(1)}))
        db.commit()

        entry = build_practice_rank(db, days=30).entries[0]
        assert entry.college_code == "25"
        assert entry.college_name == "信息工程学院"
        assert entry.major_name == "软件工程"


def test_nowcoder_main_uses_its_own_user_agent(env):
    """主站接口要用短 UA，不能用外层那个完整 Chrome UA。

    阿里云 WAF 会把完整 Chrome UA 当成浏览器流量拦下来，返回一个 HTML
    挑战页（HTTP 200，但内容是网页不是 JSON）。反直觉——越像浏览器越
    容易被拦。

    这个坑很隐蔽：单独调用主站函数时用的是简单 UA，一切正常；只有走
    sync 的完整路径（用 Chrome UA）才会失败，而且失败是静默的。
    """
    from app.platforms import _NOWCODER_API_UA, _nowcoder_main_solved

    seen: dict[str, str] = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"success": True, "code": 0, "data": {"total": 0, "records": []}}

    class FakeClient:
        def post(self, *args, **kwargs):
            seen.update(kwargs.get("headers") or {})
            return FakeResponse()

    _nowcoder_main_solved(FakeClient(), "825377486")

    assert seen.get("User-Agent") == _NOWCODER_API_UA
    assert "Chrome" not in seen.get("User-Agent", "")


def test_nowcoder_main_reports_html_response_as_error(env):
    """返回 HTML（被 WAF 拦）时要报错，不能当成「没有记录」。"""
    from app.platforms import FetchError, _nowcoder_main_solved

    class FakeResponse:
        status_code = 200
        text = "<!doctype html><meta name='aliyun_waf_aa'>"

        @staticmethod
        def json():
            raise ValueError("not json")

    class FakeClient:
        def post(self, *args, **kwargs):
            return FakeResponse()

    with pytest.raises(FetchError) as exc:
        _nowcoder_main_solved(FakeClient(), "825377486")
    assert "无法解析" in str(exc.value)


def test_sync_draws_daily_problems(env):
    """同步任务要把当天的每日一题抽出来。

    抽题原本只在有人访问 /api/daily 时发生。生成任务凌晨 4:30 跑的时候
    站点还没人访问，题目没落库，它拿到空列表就退出了——2026-10-05 漏了
    一整天。搭在同步任务上，换天后的第一次唤醒就会抽。
    """
    from app import daily as daily_mod
    from app import sync as sync_mod
    from app.db import get_engine
    from app.models import DailyProblem, ProblemCache

    today = daily_mod.workshop_today()

    with Session(get_engine()) as db:
        assert db.exec(select(DailyProblem).where(DailyProblem.day == today)).all() == []
        # 三个档各放一道候选。
        for key, rating in (("1000A", 900), ("1001A", 1200), ("1002A", 1500)):
            db.add(
                ProblemCache(
                    platform="codeforces",
                    key=key,
                    name=f"Problem {key}",
                    rating=rating,
                    solved_count=5000,
                    has_statement=True,
                    url=f"https://codeforces.com/problemset/problem/{key}",
                )
            )
        db.commit()

    # 只跑抽题那一段，不做网络同步（测试环境没有账号要抓）。
    sync_mod.run_sync()

    with Session(get_engine()) as db:
        rows = db.exec(select(DailyProblem).where(DailyProblem.day == today)).all()
    assert len(rows) == 3
    assert {r.band for r in rows} == {"800-1000", "1100-1300", "1400-1600"}
