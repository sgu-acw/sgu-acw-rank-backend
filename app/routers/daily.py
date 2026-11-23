"""每日一题的接口。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, select

from .. import daily
from ..daily import CHINA_TZ, solved_by_count
from ..db import get_session
from ..deps import get_optional_user, require_admin, require_origin_secret
from ..models import DailyProblem, DailyProblemSolve, ProblemCache, User
from ..problemlists import solved_keys
from ..problems import problem_url

router = APIRouter(tags=["daily"], dependencies=[Depends(require_origin_secret)])


class DailyProblemEntry(BaseModel):
    """每日一题里的一道。"""

    band: str
    platform: str
    key: str
    name: str
    rating: int | None = None
    url: str
    #: CF 全站有多少人过了。作为难度可信度的旁证。
    solved_count: int | None = None
    #: 站内有多少成员做对了这道题。
    members_solved: int = 0
    #: 当前用户过没过。没登录时是 False。
    solved: bool = False


class DailyProblemBoard(BaseModel):
    """某一天的每日一题。"""

    day: str
    #: 下一次换题的时刻（东八区次日零点），前端拿去显示倒计时。
    reset_at: datetime
    problems: list[DailyProblemEntry] = Field(default_factory=list)
    #: 当前用户签到了几天。
    checkin_days: int = 0
    #: 当前用户连续签到几天。
    checkin_streak: int = 0
    #: 今天签到了没。
    checked_in_today: bool = False
    #: 今天有谁签到了。
    checked_in_today_names: list[str] = Field(default_factory=list)


def _cached_map(session: Session, rows: list[DailyProblem]) -> dict[str, ProblemCache]:
    """一次把这批题的元信息查出来，避免逐题查库。"""
    if not rows:
        return {}
    keys = [row.key for row in rows]
    found = session.exec(
        select(ProblemCache).where(
            ProblemCache.platform == "codeforces",  # type: ignore[arg-type]
            ProblemCache.key.in_(keys),  # type: ignore[attr-defined]
        )
    ).all()
    return {row.key: row for row in found}


def _to_entries(
    session: Session,
    rows: list[DailyProblem],
    *,
    solved: set[tuple[str, str]] | None,
) -> list[DailyProblemEntry]:
    """把落库的记录补上题目元信息。

    题目名、rating 这些不在 ``daily_problems`` 里存第二份——``problem_cache``
    已经是权威来源，存两份迟早会不一致。
    """
    cache = _cached_map(session, rows)
    members = solved_by_count(session, rows)
    out: list[DailyProblemEntry] = []
    for row in rows:
        cached = cache.get(row.key)
        out.append(
            DailyProblemEntry(
                band=row.band,
                platform=row.platform,
                key=row.key,
                name=cached.name if cached else "",
                rating=cached.rating if cached else None,
                url=(cached.url if cached else "") or problem_url(row.platform, row.key),
                solved_count=cached.solved_count if cached else None,
                members_solved=members.get(row.key, 0),
                solved=(solved is not None and (row.platform, row.key) in solved),
            )
        )
    return out


def _checkin_stats(session: Session, user: User | None, day: str) -> dict:
    """当前用户的签到情况。未登录时全为 0。"""
    if user is None or not user.id:
        return {}

    # 一人一天可以命中多道每日一题（每题一行），累计签到要按不同的天数算。
    total = session.exec(
        select(func.count(func.distinct(DailyProblemSolve.day))).where(  # type: ignore[arg-type]
            DailyProblemSolve.user_id == user.id
        )
    ).one()

    return {
        "checkin_days": int(total or 0),
        "checkin_streak": daily.checkin_streak(session, user_id=user.id, today=day),
        "checked_in_today": bool(
            session.exec(
                select(DailyProblemSolve).where(
                    DailyProblemSolve.user_id == user.id,
                    DailyProblemSolve.day == day,
                )
            ).first()
        ),
    }


def _checked_in_names(session: Session, day: str) -> list[str]:
    """今天签到的人。公开信息，按首次过题顺序去重返回。"""
    rows = session.exec(
        select(User.id, User.display_name)
        .join(DailyProblemSolve, DailyProblemSolve.user_id == User.id)  # type: ignore[arg-type]
        .where(DailyProblemSolve.day == day)
        .order_by(DailyProblemSolve.created_at)  # type: ignore[arg-type]
    ).all()
    seen: set[int] = set()
    names: list[str] = []
    for uid, name in rows:
        if uid in seen or not name:
            continue
        seen.add(uid)
        names.append(name)
    return names


def _board(session: Session, day: str, user: User | None) -> DailyProblemBoard:
    rows = daily.ensure_daily(session, day=day)
    solved = solved_keys(session, user.id) if user and user.id else None
    return DailyProblemBoard(
        day=day,
        reset_at=daily.next_reset_at(),
        problems=_to_entries(session, rows, solved=solved),
        checked_in_today_names=_checked_in_names(session, day),
        **_checkin_stats(session, user, day),
    )


def _validate_day(day: str) -> str:
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="日期格式应为 YYYY-MM-DD"
        ) from None
    return day


@router.get("/daily", response_model=DailyProblemBoard)
def get_daily(
    day: str | None = Query(default=None, description="YYYY-MM-DD，默认今天"),
    session: Session = Depends(get_session),
    user: User | None = Depends(get_optional_user),
) -> DailyProblemBoard:
    """某一天的每日一题，默认今天。

    第一次访问某一天时现抽并落库，之后读缓存。所以这个接口是幂等的：
    同一天无论调多少次，返回的都是同一套题。
    """
    target = _validate_day(day) if day else daily.workshop_today()
    return _board(session, target, user)


@router.post(
    "/daily/redraw",
    response_model=DailyProblemBoard,
    dependencies=[Depends(require_admin)],
)
def redraw_daily(
    day: str | None = Query(default=None, description="要重抽的日期，默认今天"),
    session: Session = Depends(get_session),
    user: User | None = Depends(get_optional_user),
) -> DailyProblemBoard:
    """重抽某一天。管理员用。

    抽到的题可能不合适（太偏、题意不清、题面挂了），得有个办法换掉。
    重抽会把这一天的全换，不做单道替换——只换一道的话，另外几道
    已经有人做过了，记录就对不上。
    """
    target = _validate_day(day) if day else daily.workshop_today()
    # 带时间戳当盐：同一个日期反复重抽，每次都能换一批。
    daily.ensure_daily(
        session,
        day=target,
        force=True,
        salt=datetime.now(CHINA_TZ).isoformat(timespec="seconds"),
    )
    return _board(session, target, user)


class CheckinEntry(BaseModel):
    """签到排行榜的一行。"""

    rank: int
    user_id: int
    display_name: str
    student_id: str
    enroll_year: int
    class_label: str | None = None
    days: int
    #: 当前连续签到天数。
    streak: int = 0


class CheckinBoard(BaseModel):
    """签到天数排行榜。"""

    entries: list[CheckinEntry] = Field(default_factory=list)
    #: 一共有多少人签过到。用来解释「为什么榜单上只有这几个人」。
    total_members: int = 0


class SolverEntry(BaseModel):
    """某道题的一个通过者。"""

    rank: int
    user_id: int
    display_name: str
    student_id: str
    enroll_year: int
    class_label: str | None = None
    #: 我们记录到的时刻。不是平台上真实的通过时间，见 daily.earliest_solvers。
    solved_at: datetime


class ProblemSolvers(BaseModel):
    """某道题的通过情况。"""

    #: 难度档，如 ``800-1000``。
    band: str
    platform: str
    key: str
    name: str = ""
    rating: int | None = None
    #: 题面地址。前端不自己拼——各平台的地址格式不一样，而且会变。
    url: str = ""

    #: 通过者，按记录时间从早到晚。第一个就是「最早通过」。
    entries: list[SolverEntry] = Field(default_factory=list)


class DailySolversBoard(BaseModel):
    """当天每道题各自的通过名单。"""

    day: str
    problems: list[ProblemSolvers] = Field(default_factory=list)


@router.get("/daily/solvers", response_model=DailySolversBoard)
def get_daily_solvers(
    day: str | None = Query(default=None, description="YYYY-MM-DD，默认今天"),
    session: Session = Depends(get_session),
) -> DailySolversBoard:
    """当天每道题谁通过了，按记录时间从早到晚。

    和签到榜的区别：签到榜是「谁连续来了几天」，这个是「哪道题谁先做出来」。
    一道题没人通过就不出现在列表里。
    """
    target = _validate_day(day) if day else daily.workshop_today()
    problems = daily.daily_for_day(session, target)
    if not problems:
        return DailySolversBoard(day=target)

    grouped = daily.earliest_solvers(session, day=target)

    # 一次把所有涉及的用户查出来，避免逐条查库。
    user_ids = {row.user_id for rows in grouped.values() for row in rows}
    users = {
        user.id: user
        for user in session.exec(select(User).where(User.id.in_(user_ids))).all()  # type: ignore[attr-defined]
    } if user_ids else {}

    # 题目元信息也一次查完。
    keys = [p.key for p in problems]
    cache = {
        row.key: row
        for row in session.exec(
            select(ProblemCache).where(
                ProblemCache.platform == "codeforces",  # type: ignore[arg-type]
                ProblemCache.key.in_(keys),  # type: ignore[attr-defined]
            )
        ).all()
    }

    out: list[ProblemSolvers] = []
    for problem in problems:
        cached = cache.get(problem.key)
        entries: list[SolverEntry] = []
        for row in grouped.get(f"{problem.platform}:{problem.key}", []):
            user = users.get(row.user_id)
            if user is None:
                continue
            entries.append(
                SolverEntry(
                    rank=len(entries) + 1,
                    user_id=row.user_id,
                    display_name=user.display_name,
                    student_id=user.student_id,
                    enroll_year=user.enroll_year,
                    class_label=user.class_label,
                    solved_at=row.created_at,
                )
            )
        out.append(
            ProblemSolvers(
                band=problem.band,
                platform=problem.platform,
                key=problem.key,
                name=cached.name if cached else "",
                rating=cached.rating if cached else None,
                # 地址现算，不用缓存里的——缓存可能是旧格式，清过之后才准。
                url=problem_url(problem.platform, problem.key),
                entries=entries,
            )
        )

    return DailySolversBoard(day=target, problems=out)


@router.get("/daily/checkin", response_model=CheckinBoard)
def get_checkin_board(
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> CheckinBoard:
    """签到天数排行榜。

    只列签过到的人。一次没签过的人不占榜位——那更接近「没参加」，
    而不是「0 天」。
    """
    rows = daily.checkin_leaderboard(session, limit=limit)
    if not rows:
        return CheckinBoard()

    user_ids = [standing.user_id for standing in rows]
    users = {
        user.id: user
        for user in session.exec(select(User).where(User.id.in_(user_ids))).all()  # type: ignore[attr-defined]
    }

    entries: list[CheckinEntry] = []
    for standing in rows:
        user = users.get(standing.user_id)
        if user is None:
            continue
        entries.append(
            CheckinEntry(
                rank=len(entries) + 1,
                user_id=standing.user_id,
                display_name=user.display_name,
                student_id=user.student_id,
                enroll_year=user.enroll_year,
                class_label=user.class_label,
                days=standing.days,
                streak=standing.streak,
            )
        )

    return CheckinBoard(entries=entries, total_members=len(entries))
