"""每日练习量排行。

数据来自 ``daily_progress``，那是同步任务算好的，这里只做查询和汇总，
不再实时访问平台。

汇总口径有两个容易踩的点：

* 一个成员在同一平台可以绑多个账号（主号加小号），所以要按人合并，
  而不是按账号各占一行。
* 「今天」按东八区算。CF 和 AtCoder 的时间戳是 UTC，成员心里的今天
  是北京时间。
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, select

from ..db import get_session
from ..deps import get_current_user, require_origin_secret
from ..models import (
    Account,
    DailyProgress,
    Platform,
    RefreshQuota,
    SyncRun,
    User,
    UserStatus,
    utcnow,
)
from ..schemas import (
    DailyPracticeEntry,
    DailyPracticeResponse,
    DashboardAccount,
    DashboardSummary,
    PracticeDay,
    PracticeFilterOptions,
    RatingEntry,
    RatingResponse,
    SyncStatus,
)
from ..sync import current_practice_day, sync_account, sync_rating

router = APIRouter(
    prefix="/practice",
    tags=["practice"],
    dependencies=[Depends(require_origin_secret)],
)


#: 「历史综合」窗口的起点。
#:
#: 不是「最近 N 天」——社团的记录从 2024 级开始，再往前是空白的，按天数算
#: 会拉出一段没有意义的空白期。
#:
#: 取 2 月 1 日而不是 1 月 1 日：前端按学期分桶，而 1 月属于上一年的秋季
#: 学期。从 1 月开始会切出一个只有 31 天的「2023 秋」，那个桶的数据必然
#: 偏少，看起来像那学期没人练。从春季学期开头算起，每个桶都是完整的。
HISTORY_START = "2024-02-01"


def _recent_dates(days: int) -> list[str]:
    """最近 ``days`` 个练习日（含今天），从早到晚。

    练习日的定义在 app/sync.py：以东八区凌晨 4 点为界，所以凌晨 0 到 3 点
    访问时，「今天」仍然是昨天那个练习日。

    ``days`` 传 ``0`` 表示「历史综合」：从 :data:`HISTORY_START` 到今天，
    有多少天就多少天。
    """
    today = date.fromisoformat(current_practice_day())

    if days <= 0:
        start = date.fromisoformat(HISTORY_START)
        span = (today - start).days
        if span < 0:
            return [today.strftime("%Y-%m-%d")]
        return [(start + timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(span + 1)]

    return [
        (today - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(days - 1, -1, -1)
    ]


def build_practice_rank(
    db: Session,
    *,
    days: int = 7,
    platform: str | None = None,
    include_retired: bool = False,
    college: str | None = None,
    enroll_year: int | None = None,
    q: str | None = None,
) -> DailyPracticeResponse:
    """按最近 N 天的过题总数排行。

    这是个普通函数，不是路由。**必须有这样一层**：路由里的 ``Query(default=...)``
    只有 FastAPI 会解析，直接调用那个函数时拿到的是 ``Query`` 对象本身，后面
    一句 ``q.strip()`` 就会炸成 500。dashboard 直接复用了排序逻辑，就这么踩过。

    筛选在汇总之后做，不是查库时做。因为筛的是「学院」「年级」这类用户的
    属性，而 ``daily_progress`` 只记账号和日期，按人合并之后才能筛。代价是
    要把该窗口的记录都读出来——但一个社团的量级下这远不是瓶颈。
    """
    dates = _recent_dates(days)
    since = dates[0]

    # 筛选选项从「有绑定账号的人」里统计，与当前时间窗无关。
    #
    # 否则选「今日」时（经常没人做题）下拉里空空的，想按学院筛都无从下手。
    # 另外这样选完一个学院，其他学院也还在下拉里，不用先清掉才能换。
    #
    # 必须在下面的早返回之前算，否则空窗口会连带把选项也清空。
    bound_user_ids = select(Account.user_id).where(Account.verified_at.is_not(None))  # type: ignore[union-attr]
    option_users = list(
        db.exec(select(User).where(User.id.in_(bound_user_ids))).all()  # type: ignore[attr-defined]
    )
    options = PracticeFilterOptions(
        colleges=sorted({u.college_name for u in option_users if u.college_name}),
        enroll_years=sorted({u.enroll_year for u in option_users if u.enroll_year}, reverse=True),
    )

    statement = select(DailyProgress).where(DailyProgress.date >= since)
    if platform:
        if platform not in Platform.ALL:
            raise HTTPException(status_code=400, detail="不支持的平台")
        statement = statement.where(DailyProgress.platform == platform)
    rows = list(db.exec(statement).all())

    if not rows:
        return DailyPracticeResponse(
            days=days,
            platform=platform,
            dates=dates,
            entries=[],
            generated_at=int(utcnow().timestamp()),
            filters=options,
        )

    # 账号 → 人。一次查完，避免逐行查库。
    account_ids = {row.account_id for row in rows}
    accounts = {
        account.id: account
        for account in db.exec(select(Account).where(Account.id.in_(account_ids))).all()  # type: ignore[attr-defined]
    }
    user_ids = {account.user_id for account in accounts.values()}
    users = {
        user.id: user
        for user in db.exec(select(User).where(User.id.in_(user_ids))).all()  # type: ignore[attr-defined]
    }

    # 按人汇总：同一天多个账号的过题数相加。
    per_user: dict[int, dict[str, int]] = {}
    per_user_total: dict[int, int] = {}
    for row in rows:
        account = accounts.get(row.account_id)
        if account is None:
            continue
        user = users.get(account.user_id)
        if user is None:
            continue
        if not include_retired and user.status != UserStatus.ACTIVE:
            continue

        bucket = per_user.setdefault(user.id, {})
        bucket[row.date] = bucket.get(row.date, 0) + row.solved_count
        per_user_total[user.id] = per_user_total.get(user.id, 0) + row.solved_count

    entries: list[DailyPracticeEntry] = []
    for user_id, by_date in per_user.items():
        user = users[user_id]
        entries.append(
            DailyPracticeEntry(
                user_id=user_id,
                display_name=user.display_name,
                student_id=user.student_id,
                enroll_year=user.enroll_year,
                college_code=user.college_code,
                major_code=user.major_code,
                college_name=user.college_name,
                major_name=user.major_name,
                class_label=user.class_label,
                total=per_user_total.get(user_id, 0),
                days=[PracticeDay(date=d, count=by_date.get(d, 0)) for d in dates],
            )
        )

    if college:
        entries = [e for e in entries if e.college_name == college]
    if enroll_year is not None:
        entries = [e for e in entries if e.enroll_year == enroll_year]
    if q and q.strip():
        needle = q.strip().lower()
        entries = [
            e for e in entries if needle in e.display_name.lower() or needle in e.student_id.lower()
        ]

    # 总量降序；并列时看最近一天，还是并列就按年级和姓名稳定排序。
    entries.sort(
        key=lambda e: (
            -e.total,
            -(e.days[-1].count if e.days else 0),
            e.enroll_year,
            e.display_name,
        )
    )
    for index, entry in enumerate(entries, start=1):
        entry.rank = index

    return DailyPracticeResponse(
        days=days,
        platform=platform,
        dates=dates,
        entries=entries,
        generated_at=int(utcnow().timestamp()),
        filters=options,
    )


@router.get("/rank", response_model=DailyPracticeResponse)
def practice_rank(
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_session),
    days: int = Query(default=7, ge=0, le=3650, description="统计最近多少天；0 表示历史综合"),
    platform: str | None = Query(default=None, description="只统计某个平台"),
    include_retired: bool = Query(default=False),
    college: str | None = Query(default=None, description="只看某个学院（学院名）"),
    enroll_year: int | None = Query(default=None, description="只看某个年级"),
    q: str | None = Query(default=None, description="按姓名或学号模糊搜索"),
) -> DailyPracticeResponse:
    """按最近 N 天的过题总数排行。需要登录。

    只做参数声明，实际计算在 :func:`build_practice_rank`。
    """
    return build_practice_rank(
        db,
        days=days,
        platform=platform,
        include_retired=include_retired,
        college=college,
        enroll_year=enroll_year,
        q=q,
    )


@router.get("/status", response_model=SyncStatus)
def sync_status(
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> SyncStatus:
    """上一次同步的情况，用于在页面上提示数据新旧。"""
    last = db.exec(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)).first()  # type: ignore[attr-defined]
    if last is None:
        return SyncStatus(has_run=False)

    import json

    try:
        errors = json.loads(last.errors_json)
    except json.JSONDecodeError:
        errors = []

    return SyncStatus(
        has_run=True,
        started_at=last.started_at,
        finished_at=last.finished_at,
        account_count=last.account_count,
        ok_count=last.ok_count,
        error_count=last.error_count,
        errors=errors,
    )


# --------------------------------------------------------------------------- #
# 手动刷新
# --------------------------------------------------------------------------- #


def _current_week_start() -> str:
    """本周的周一，YYYY-MM-DD（东八区）。

    按周记账的好处是换周自然重置，不需要额外的清理任务。
    """
    today = date.fromisoformat(current_practice_day())
    return (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")


@router.get("/quota")
def refresh_quota(
    current: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    """本周还剩几次手动刷新机会。"""
    week = _current_week_start()
    row = db.exec(
        select(RefreshQuota).where(
            RefreshQuota.user_id == current.id,
            RefreshQuota.week_start == week,
        )
    ).first()
    used = row.used if row else 0
    return {
        "week_start": week,
        "limit": RefreshQuota.WEEKLY_LIMIT,
        "used": used,
        "remaining": max(0, RefreshQuota.WEEKLY_LIMIT - used),
    }


@router.post("/refresh")
def manual_refresh(
    current: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    """立刻抓一次自己绑定的账号，不等定时任务。

    抓取本身是定时任务在做，成员不需要操心。但有人刚打完一场想立刻看到
    数字变化，所以给一个口子。每周七次，防止有人反复点把平台惹毛。

    额度在真正开始抓之前扣掉，抓失败也不退：失败往往是平台限流，退了额度
    只会让人反复重试，反而更容易被限。
    """
    # 先确认有已验证的账号，避免空账号白白扣掉每周额度。
    accounts = list(
        db.exec(
            select(Account).where(
                Account.user_id == current.id,
                Account.verified_at.is_not(None),  # type: ignore[union-attr]
            )
        ).all()
    )
    if not accounts:
        raise HTTPException(status_code=400, detail="请先绑定并验证至少一个平台账号")

    week = _current_week_start()
    # 原子初始化 + 条件自增额度，确保并发点击不会超出每周上限。
    db.exec(
        insert(RefreshQuota)
        .values(user_id=current.id, week_start=week, used=0)
        .on_conflict_do_nothing(index_elements=["user_id", "week_start"])
    )
    claimed = db.exec(
        update(RefreshQuota)
        .where(
            RefreshQuota.user_id == current.id,
            RefreshQuota.week_start == week,
            RefreshQuota.used < RefreshQuota.WEEKLY_LIMIT,
        )
        .values(used=RefreshQuota.used + 1)
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(
            status_code=429,
            detail=f"本周的手动刷新次数已用完（{RefreshQuota.WEEKLY_LIMIT} 次），下周一重置",
        )
    quota = db.exec(
        select(RefreshQuota).where(
            RefreshQuota.user_id == current.id,
            RefreshQuota.week_start == week,
        )
    ).one()
    remaining = max(0, RefreshQuota.WEEKLY_LIMIT - quota.used)
    db.commit()

    account_ids = [a.id for a in accounts if a.id is not None]
    synced = 0
    added = 0
    failures: list[str] = []
    for account_id in account_ids:
        account = db.get(Account, account_id, populate_existing=True)
        if account is None:
            continue
        ok, detail, count = sync_account(db, account, force=True)
        db.commit()
        if ok:
            synced += 1
            added += count
        else:
            failures.append(f"{Platform.LABELS.get(account.platform, account.platform)}: {detail}")
        # rating 也顺手刷新，独立提交避免在外部请求期间持有写锁。
        account = db.get(Account, account_id, populate_existing=True)
        if account is not None:
            sync_rating(db, account, force=True)
            db.commit()

    return {
        "synced": synced,
        "added": added,
        "failures": failures,
        "remaining": remaining,
    }


# --------------------------------------------------------------------------- #
# rating 榜
# --------------------------------------------------------------------------- #


@router.get("/rating", response_model=RatingResponse)
def rating_board(
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_session),
    platform: str = Query(default=Platform.CODEFORCES),
    include_retired: bool = Query(default=False),
) -> RatingResponse:
    """某个平台的 rating 榜。

    这是旧 Codeforces rank 页的延续：那时 rating 由前端直接问 CF 拿，现在
    后端每天缓存一次，页面只读缓存。

    以账号为单位而不是以人为单位——一个人可以有主号和小号，两个号各有各的
    rating，合并成一个数没有意义。

    不提供「全部平台」：各平台的 rating 不在同一把尺子上（CF 的 1600 和
    洛谷的 1600 完全不是一回事），混在一起排序会误导人。
    """
    if platform not in Platform.ALL:
        raise HTTPException(status_code=400, detail="不支持的平台")

    statement = select(Account).where(
        Account.platform == platform,
        Account.verified_at.is_not(None),  # type: ignore[union-attr]
    )
    accounts = list(db.exec(statement).all())

    user_ids = {a.user_id for a in accounts}
    users = {
        user.id: user
        for user in db.exec(select(User).where(User.id.in_(user_ids))).all()  # type: ignore[attr-defined]
    }

    entries: list[RatingEntry] = []
    for account in accounts:
        user = users.get(account.user_id)
        if user is None:
            continue
        if not include_retired and user.status != UserStatus.ACTIVE:
            continue
        # 还没抓到 rating 的账号先不列出，否则会占着位置显示一个空值。
        if account.rating is None:
            continue

        entries.append(
            RatingEntry(
                user_id=user.id,
                display_name=user.display_name,
                student_id=user.student_id,
                enroll_year=user.enroll_year,
                college_code=user.college_code,
                major_code=user.major_code,
                college_name=user.college_name,
                major_name=user.major_name,
                class_label=user.class_label,
                platform=account.platform,
                identifier=account.identifier,
                platform_name=account.display_name,
                rating=account.rating,
                max_rating=account.max_rating,
                fetched_at=account.rating_fetched_at,
            )
        )

    # 当前 rating 降序；并列看历史最高，再并列按年级和姓名稳定排序。
    entries.sort(
        key=lambda e: (
            -(e.rating or 0),
            -(e.max_rating or 0),
            e.enroll_year,
            e.display_name,
        )
    )
    for index, entry in enumerate(entries, start=1):
        entry.rank = index

    return RatingResponse(
        platform=platform,
        label=Platform.LABELS[platform],
        entries=entries,
        generated_at=int(utcnow().timestamp()),
    )


# --------------------------------------------------------------------------- #
# 个人数据面板
# --------------------------------------------------------------------------- #


@router.get("/dashboard", response_model=DashboardSummary)
def dashboard(
    current: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> DashboardSummary:
    """首页用的个人数据面板。

    一次请求给齐所有数字，而不是让前端分别去拉榜单再筛自己——那样首页会
    开好几个请求、各自闪一下，而且要把别人的数据也传下来。

    名次按最近 7 天的过题总量算，与练习榜的口径保持一致。
    """
    today = current_practice_day()
    window = _recent_dates(30)
    since = window[0]

    accounts = list(
        db.exec(
            select(Account)
            .where(Account.user_id == current.id)
            .order_by(Account.platform, Account.created_at)  # type: ignore[attr-defined]
        ).all()
    )

    verified_account_ids = [a.id for a in accounts if a.verified_at is not None]
    rows: list[DailyProgress] = []
    active_dates: set[str] = set()
    if verified_account_ids:
        rows = list(
            db.exec(
                select(DailyProgress).where(
                    DailyProgress.account_id.in_(verified_account_ids),  # type: ignore[attr-defined]
                    DailyProgress.date >= since,
                )
            ).all()
        )
        active_dates = set(
            db.exec(
                select(DailyProgress.date).where(
                    DailyProgress.account_id.in_(verified_account_ids),  # type: ignore[attr-defined]
                    DailyProgress.solved_count > 0,
                )
            ).all()
        )

    # 自己这 30 天，按天合并（主号小号相加）。
    by_date: dict[str, int] = {}
    for row in rows:
        by_date[row.date] = by_date.get(row.date, 0) + row.solved_count

    # 计算当前连续刷题天数（今天尚未做题时，从昨天往前数，不提前清零）。
    streak_cursor = date.fromisoformat(today)
    if today not in active_dates:
        streak_cursor -= timedelta(days=1)
    practice_streak = 0
    while streak_cursor.isoformat() in active_dates:
        practice_streak += 1
        streak_cursor -= timedelta(days=1)

    week_start = _recent_dates(7)[0]
    week_total = sum(count for date, count in by_date.items() if date >= week_start)
    today_total = by_date.get(today, 0)
    window_total = sum(by_date.values())

    # 名次：拿最近 7 天的榜单，找自己在哪。
    rank_value: int | None = None
    rank_total = 0
    board = build_practice_rank(db, days=7)
    rank_total = len(board.entries)
    for entry in board.entries:
        if entry.user_id == current.id:
            rank_value = entry.rank
            break

    quota_row = db.exec(
        select(RefreshQuota).where(
            RefreshQuota.user_id == current.id,
            RefreshQuota.week_start == _current_week_start(),
        )
    ).first()
    used = quota_row.used if quota_row else 0

    return DashboardSummary(
        practice_day=today,
        today=today_total,
        week=week_total,
        total=window_total,
        practice_streak=practice_streak,
        days=[PracticeDay(date=d, count=by_date.get(d, 0)) for d in window],
        practice_rank=rank_value,
        practice_total=rank_total,
        accounts=[
            DashboardAccount(
                platform=a.platform,
                label=Platform.LABELS.get(a.platform, a.platform),
                identifier=a.identifier,
                platform_name=a.display_name,
                verified=a.verified_at is not None,
                rating=a.rating,
                max_rating=a.max_rating,
                sync_flag=a.sync_flag,
                consecutive_failures=a.consecutive_failures,
                last_error=a.last_error,
            )
            for a in accounts
        ],
        refresh_remaining=max(0, RefreshQuota.WEEKLY_LIMIT - used),
        refresh_limit=RefreshQuota.WEEKLY_LIMIT,
    )
