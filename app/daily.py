"""每日一题。

每天几道由 BANDS 决定，每个难度档各抽一道。

**怎么保证抽到的题难度不是乱标的**

CF 的 rating 是官方标的，但不是每道题都靠谱：早年有些题参与人数很少，
rating 是拿几十个人的成绩估出来的，误差能有好几百。这种题抽出来，写着
1400 实际做起来像 1800，一天就废了。

解法是拿 ``solved_count``（多少人过了）当可信度门槛。过的人越多，rating
越接近真实难度。默认门槛 2000 人，在这个门槛下（2026-10 的题库）：

    800-1000   1388 题
    1100-1300  1078 题
    1400-1600  1126 题
    1700-2100  1599 题

每档都有上千题可选，抽签空间足够，不用为了数量放宽标准。

**「2024 年之前」怎么判定**

用 contest id 卡：CF 的比赛 id 是单调递增的，2024-01-01 之前办的比赛最大
id 是 1917，之后最小是 1918。实测没有反例，比拿时间戳逐个比对简单，也
不用为了判定去拉一遍比赛列表。

**保留** ``contest_id < 1918`` 的题，即 2024 年之前的。

为什么不要新的：新比赛的参赛人数和 rating 分布还在形成中，标出来的分
与实际难度对不上。老题经过足够多人的检验，标多少就是多少。

**还要有题面**

``problem_cache.has_statement`` 为假的题不进池子。题面存在离线索引里
（生成题解要用），覆盖到某个时间点为止。
用 ``python -m app.import_statements`` 从索引回填这个字段。

**抽签怎么做到「同一天同一套」**

候选池按题目 key 排序固定顺序，再用 ``day`` 当种子做确定性洗牌，每档取头一道。
同一天无论谁访问、访问多少次，算出来都是同一套——这是「全站统一题目」的
依据，不需要额外的协调。

抽中的题写进 ``daily_problems``，之后只读这张表。落库不是为了「记住抽过
什么」（题目允许重复出现），而是为了让当天这套题在题库变动时也保持不变。
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from . import daytime
from .models import (
    Account,
    DailyProblem,
    DailyProblemSolve,
    Editorial,
    ProblemCache,
    SolvedSnapshot,
    utcnow,
)

#: 东八区。换题时刻和出题日期都按它算。
#:
#: 「一天」的定义来自 ``daytime``，与练习数据共用同一套——两处各写各的
#: 会出现「练习数据算昨天、每日一题算今天」的错位。
CHINA_TZ = daytime.CST

#: 换题时刻，东八区凌晨 4 点。比赛打到半夜的人不该看到题目突然换了。
RESET_HOUR = daytime.RESET_HOUR

#: 难度档位。键是接口里用的名字，值是 CF rating 的闭区间。
BANDS: dict[str, tuple[int, int]] = {
    "800-1000": (800, 1000),
    "1100-1300": (1100, 1300),
    "1400-1600": (1400, 1600),
    "1700-2100": (1700, 2100),
}

#: 档位在界面上的显示顺序。dict 保序，但显式写出来更清楚。
BAND_ORDER: tuple[str, ...] = ("800-1000", "1100-1300", "1400-1600", "1700-2100")

#: 2024-01-01 之后举办的比赛，id 从 1918 开始。
#: 实测 2154 场比赛里没有反例（见模块开头）。
CONTESTS_FROM_2024 = 1918

#: 可信度门槛：过题人数少于这个值的题不参与抽签。
#:
#: 2000 是在「够可信」和「够多」之间取的。再往上（比如 5000）可信度提升
#: 有限，但 1400-1600 档会掉到 908 题；再往下（比如 1000）会放进一批
#: rating 波动较大的题，而题量本来就不缺。
MIN_SOLVED_COUNT = 2000

#: 一次抽几道。等于档位数，每档一道。
PER_DAY = 4

_CF_KEY_RE = re.compile(r"^(\d+)")


def _contest_id(key: str) -> int | None:
    """从 CF 题号里取比赛 id。``1918A`` → 1918，取不到返回 None。"""
    match = _CF_KEY_RE.match(key)
    return int(match.group(1)) if match else None


def workshop_today(now: datetime | None = None) -> str:
    """今天出的是哪一套题，格式 YYYY-MM-DD。

    以凌晨 4 点为界，与练习数据同一套口径：0 点到 3 点 59 分仍算作
    「昨天」。这么定是因为比赛经常打到半夜，那时候还在做题的人不该
    突然看到题目换了。
    """
    moment = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return daytime.day_of(moment)


@dataclass(frozen=True)
class CheckinStanding:
    """某人的签到战绩。"""

    user_id: int
    #: 累计签到天数。
    days: int
    #: 当前连续签到天数。
    streak: int


@dataclass(frozen=True)
class Candidate:
    """候选题目。"""

    key: str
    name: str
    rating: int
    solved_count: int
    url: str


def load_candidates(session: Session) -> dict[str, list[Candidate]]:
    """从题目缓存里挑出每个档位的候选池。

    只认 CF、有 rating、有 solvedCount、且是 2024 年之前的题。

    ``solved_count`` 由 ``fetch_all_codeforces`` 从 ``problemStatistics``
    抓回来。老数据里这一列是空的，需要重跑一次全量抓取才有值。
    """
    rows = session.exec(
        select(ProblemCache).where(ProblemCache.platform == "codeforces")
    ).all()

    pools: dict[str, list[Candidate]] = {band: [] for band in BANDS}
    for row in rows:
        rating = row.rating
        if rating is None:
            continue

        contest_id = _contest_id(row.key)
        # 只要 2024 年之前的。
        #
        # 2024 年之后的题 rating 掉价严重：新比赛的参赛人数和 rating 分布
        # 还在形成中，标出来的分跟实际难度对不上。老题经过了足够多人的检验，
        # 标多少就是多少。
        if contest_id is None or contest_id >= CONTESTS_FROM_2024:
            continue

        solved = row.solved_count
        if solved is None or solved < MIN_SOLVED_COUNT:
            continue

        # 没题面的题不抽。
        #
        # 题解要靠题面才能写，而题面索引来自一份离线数据集，覆盖到某个
        # 时间点为止。CF 持续出新比赛，新题不在索引里。抽到这种题的话，
        # 当天就只能写「生成失败」——不如一开始就不抽。
        if not row.has_statement:
            continue

        for band, (low, high) in BANDS.items():
            if low <= rating <= high:
                pools[band].append(
                    Candidate(
                        key=row.key,
                        name=row.name,
                        rating=rating,
                        solved_count=solved,
                        url=row.url,
                    )
                )
                break

    # 按 key 排序固定顺序，抽签才有确定性。
    for band in pools:
        pools[band].sort(key=lambda c: _sort_key(c.key))
    return pools


def _sort_key(key: str) -> tuple[int, str]:
    """按「比赛 id 数字 + 题号字母」排，让 1000A 排在 999B 后面。

    直接按字符串排的话 ``"1000A" < "999B"``，顺序会乱。
    """
    contest_id = _contest_id(key)
    if contest_id is None:
        return (1 << 30, key)
    return (contest_id, key[len(str(contest_id)) :])


def _seed_for(day: str, band: str, salt: str = "") -> str:
    """抽签种子。

    同一天同一档位固定，跨天跨档位不同。``salt`` 只有重抽时才带——
    不加盐的话，重抽会用同一个种子算出同一套题，等于没抽。
    """
    base = f"sgu-acw-daily:{day}:{band}"
    return f"{base}:{salt}" if salt else base


def pick_for_band(
    pool: list[Candidate],
    *,
    day: str,
    band: str,
    salt: str = "",
) -> Candidate | None:
    """从池子里挑一道。

    用「种子 + 洗牌」而不是随机取下标：洗牌后的顺序只取决于种子和池子内容，
    所以同一天永远得到同一道题。

    题目允许重复出现——今天抽到的题，过些天再抽到是正常的。不记「抽过
    哪些」的话逻辑简单很多，也不会因为池子被抽空而需要重置。
    """
    if not pool:
        return None

    rng = random.Random(_seed_for(day, band, salt))
    shuffled = list(pool)
    rng.shuffle(shuffled)
    return shuffled[0]


def ensure_daily(
    session: Session,
    *,
    day: str | None = None,
    force: bool = False,
    salt: str = "",
) -> list[DailyProblem]:
    """保证某一天的题都在库里，返回它们。

    ``force=True`` 时重抽（管理页用），否则已有记录直接返回。

    ``salt`` 只在重抽时有意义：不加盐的话种子不变，算出来还是同一套题。
    抽签结果会连同种子一起落库，所以重抽之后仍然是可复现的。
    """
    target = day or workshop_today()

    existing = session.exec(
        select(DailyProblem).where(DailyProblem.day == target)
    ).all()
    if existing and not force:
        return sorted_by_band(existing)

    if force:
        # 先把挂在旧题上的题解删掉。
        #
        # 题解的外键指向 daily_problems，不先删的话重抽会撞上外键约束
        # 直接 500。而重抽本来就是要换掉这一天的题，旧的题解对新题没有
        # 意义——留着还会因为 key 对不上而在接口里串味。
        #
        # 必须显式 flush：SQLAlchemy 的 unit of work 不保证按 add/delete
        # 的先后顺序发语句，不 flush 的话它可能先删 daily_problems，
        # 那就又撞上外键了。
        old_ids = [row.id for row in existing if row.id]
        if old_ids:
            for editorial in session.exec(
                select(Editorial).where(Editorial.daily_problem_id.in_(old_ids))  # type: ignore[attr-defined]
            ).all():
                session.delete(editorial)
            session.flush()

        for row in existing:
            session.delete(row)
        session.commit()

    pools = load_candidates(session)

    created: list[DailyProblem] = []
    for band in BAND_ORDER:
        pool = pools.get(band) or []
        candidate = pick_for_band(pool, day=target, band=band, salt=salt)
        if candidate is None:
            continue
        row = DailyProblem(
            day=target,
            band=band,
            platform="codeforces",
            key=candidate.key,
            seed=_seed_for(target, band, salt),
        )
        session.add(row)
        created.append(row)

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        if force:
            raise
        existing = session.exec(
            select(DailyProblem).where(DailyProblem.day == target)
        ).all()
        return sorted_by_band(existing)
    for row in created:
        session.refresh(row)
    return sorted_by_band(created)


def sorted_by_band(rows: list[DailyProblem]) -> list[DailyProblem]:
    """按档位顺序排，前端不用再排。"""
    order = {band: index for index, band in enumerate(BAND_ORDER)}
    return sorted(rows, key=lambda r: order.get(r.band, 99))


def daily_for_day(db: Session, day: str) -> list[DailyProblem]:
    """某一天的每日一题，按档位排好。

    只读，不抽题——要「没有就抽出来」用 ``ensure_daily``。
    """
    rows = db.exec(select(DailyProblem).where(DailyProblem.day == day)).all()
    return sorted_by_band(rows)


def next_reset_at(now: datetime | None = None) -> datetime:
    """下一次换题的时刻（东八区凌晨 4 点）。

    前端拿这个显示倒计时，省得自己算时区。
    """
    return daytime.next_reset(now)


def backfill_days(session: Session, *, days: int, end: str | None = None) -> int:
    """补抽过去若干天的题。

    上线时用：不补的话，历史日期都是空的，界面上看着像坏了。补的时候按
    时间顺序一天天来，保证「抽过的题不再抽」这个约束在历史区间里也成立。
    """
    last = date.fromisoformat(end) if end else date.fromisoformat(workshop_today())
    made = 0
    for offset in range(days - 1, -1, -1):
        day = (last - timedelta(days=offset)).isoformat()
        rows = ensure_daily(session, day=day)
        made += len(rows)
    return made


# --------------------------------------------------------------------------- #
# 签到
# --------------------------------------------------------------------------- #


def record_solves(
    db: Session,
    *,
    user_id: int,
    account_id: int | None,
    day: str,
    solved_keys: set[str],
) -> int:
    """比对某人已过的题与某天的每日一题，把命中的都记下来。

    ``solved_keys`` 是这个人所有账号的已过题目（平台 + 题号），与题单
    完成度的口径一致。

    返回这次**新记了几条**。已经记过的不重复写——同一天多次同步是常态。

    一个人一天可能命中多道（四道题做了三道就记三条）。每条各记各的，
    这样「每道题谁最早通过」才有数据可查。
    """
    today_problems = db.exec(
        select(DailyProblem).where(DailyProblem.day == day)
    ).all()
    if not today_problems:
        # 那天的题还没抽（或者抽不出来），没什么可比的。
        return 0

    hits = [row for row in today_problems if (row.platform, row.key) in solved_keys]
    if not hits:
        return 0

    # 已经记过的题号。一次查完，别在循环里逐个查。
    existing = {
        (row.platform, row.key)
        for row in db.exec(
            select(DailyProblemSolve).where(
                DailyProblemSolve.user_id == user_id,
                DailyProblemSolve.day == day,
            )
        ).all()
    }

    added = 0
    for hit in hits:
        if (hit.platform, hit.key) in existing:
            continue
        db.add(
            DailyProblemSolve(
                user_id=user_id,
                day=day,
                platform=hit.platform,
                key=hit.key,
                account_id=account_id,
            )
        )
        added += 1

    if added:
        db.commit()
    return added


def solved_by_count(db: Session, rows: list[DailyProblem]) -> dict[str, int]:
    """每道题有多少站内成员做对了。

    数的是「有过这道题的人数」，不是提交次数——一个人在同一道题上提交
    多次只算一个。

    口径与题单完成度一致：看 ``solved_snapshots``，同一平台的主号小号
    取并集。只统计已绑账号的成员。
    """
    if not rows:
        return {}

    wanted = {(row.platform, row.key) for row in rows}
    platforms = list({row.platform for row in rows})
    counts = dict.fromkeys((row.key for row in rows), 0)

    # 每个账号一份快照，按人合并；只统计相关平台的已验证账号。
    snapshots = db.exec(
        select(SolvedSnapshot, Account)
        .join(Account, Account.id == SolvedSnapshot.account_id)  # type: ignore[arg-type]
        .where(
            Account.verified_at.is_not(None),  # type: ignore[union-attr]
            Account.platform.in_(platforms),  # type: ignore[attr-defined]
        )
    ).all()

    per_user: dict[int, set[tuple[str, str]]] = {}
    for snapshot, account in snapshots:
        try:
            keys = json.loads(snapshot.problems_json)
        except (TypeError, ValueError):
            continue
        bucket = per_user.setdefault(account.user_id, set())
        for key in keys:
            bucket.add((account.platform, str(key)))

    for keys in per_user.values():
        for pair in keys & wanted:
            counts[pair[1]] = counts.get(pair[1], 0) + 1

    return counts


def _streak_from_days(days: set[str], *, today: str) -> int:
    """从一组日期算连续签到天数。

    从今天往前数，断一天就停。今天还没签不算断——一天还没过完，此时把
    连续数归零会让人以为白签了，于是从昨天开始数。
    """
    if not days:
        return 0

    start = date.fromisoformat(today)
    if today not in days:
        start -= timedelta(days=1)

    streak = 0
    cursor = start
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def checkin_leaderboard(
    db: Session, *, limit: int = 50, today: str | None = None
) -> list[CheckinStanding]:
    """签到排行榜。按连续天数降序，连续相同再看累计。

    **为什么按连续而不是累计排**：累计天数一旦堆起来就不会掉，签了半年
    的人哪怕两个月没来也一直挂在前面，榜单就定死了。连续天数会掉——今天
    没签，明天就归零。榜单因此每天都是活的，这才有「签到」的意思。

    连续天数依赖「今天是哪天」，没法在 SQL 里算，只能把每个人的签到日期
    取出来在 Python 里数。签到的日期集合通常很小（一年也就 365 行/人），
    这个开销可以接受；真要撑到几万人的量级，再考虑把连续天数落库。
    """
    # 一次查询取完所有人的签到日期，避免逐人查库。
    rows = db.exec(select(DailyProblemSolve.user_id, DailyProblemSolve.day)).all()
    if not rows:
        return []

    per_user: dict[int, set[str]] = {}
    for user_id, day in rows:
        per_user.setdefault(int(user_id), set()).add(day)

    current = today or workshop_today()
    standings = [
        CheckinStanding(
            user_id=user_id,
            days=len(days),
            streak=_streak_from_days(days, today=current),
        )
        for user_id, days in per_user.items()
    ]

    # 连续降序；连续相同按累计降序；再相同按 id 升序，保证顺序稳定
    # （否则两次请求的排名可能不一样，看着像在跳）。
    standings.sort(key=lambda item: (-item.streak, -item.days, item.user_id))
    return standings[:limit]


def earliest_solvers(
    db: Session, *, day: str | None = None
) -> dict[str, list[DailyProblemSolve]]:
    """某天每道题谁通过了，按记录时间从早到晚。

    返回「平台:题号」→ 记录列表。没有通过的题不出现在结果里。

    **时间精度是同步间隔**。``created_at`` 是同步任务发现这道题已通过的
    时刻，不是平台上真正的通过时间——CF 的 API 只给「首次通过的秒级时间
    戳」，那个没存进这张表。同步每 5 分钟一批，所以排序只在同一批内可靠，
    跨批次能分出先后。

    真要精确到秒，得把平台上给的时间戳一起存下来。洛谷连时间戳都不给
    （``fetch_solved_luogu`` 一律给 0），那条路走不通，所以统一用记录
    时间，至少口径一致。
    """
    target = day or workshop_today()

    rows = db.exec(
        select(DailyProblemSolve)
        .where(DailyProblemSolve.day == target)
        .order_by(DailyProblemSolve.created_at)  # type: ignore[arg-type]
    ).all()

    grouped: dict[str, list[DailyProblemSolve]] = {}
    for row in rows:
        grouped.setdefault(f"{row.platform}:{row.key}", []).append(row)
    return grouped


def checkin_streak(db: Session, *, user_id: int, today: str | None = None) -> int:
    """某人当前连续签到天数。"""
    days = set(
        db.exec(
            select(DailyProblemSolve.day).where(DailyProblemSolve.user_id == user_id)
        ).all()
    )
    return _streak_from_days(days, today=today or workshop_today())


__all__ = [
    "BANDS",
    "BAND_ORDER",
    "CHINA_TZ",
    "MIN_SOLVED_COUNT",
    "Candidate",
    "CheckinStanding",
    "backfill_days",
    "checkin_leaderboard",
    "checkin_streak",
    "daily_for_day",
    "ensure_daily",
    "load_candidates",
    "next_reset_at",
    "pick_for_band",
    "record_solves",
    "solved_by_count",
    "workshop_today",
]
