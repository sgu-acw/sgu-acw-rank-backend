"""题解接口。

两组路由：

- ``/daily/editorials``：给成员看的。只返回已公布的内容，未公布的部分
  在服务端裁掉——放在响应里再靠前端隐藏，打开开发者工具就能看到答案。
- ``/internal/editorials``：给生成器（pi）用的。带独立密钥，能写、能查
  还没公布的草稿、能报告失败。

**为什么生成不放在后端**

生成要读题面、可能跑代码验证、失败要重试，还要能被人盯着调。这些都需要
工具调用和长会话，塞进 web 请求只会超时。后端只做存储和鉴权，生成交给
一个能跑几分钟的外部进程。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from .. import daily as daily_mod
from .. import editorials
from ..db import get_session
from ..deps import (
    get_optional_user,
    require_admin,
    require_editorial_secret,
    require_origin_secret,
)
from ..models import DailyProblem, Editorial, EditorialStatus, ProblemCache, User
from ..problems import problem_url

router = APIRouter(tags=["editorials"], dependencies=[Depends(require_origin_secret)])


# --------------------------------------------------------------------------- #
# 给成员看的
# --------------------------------------------------------------------------- #


class EditorialView(BaseModel):
    """一篇题解对成员的形态。未公布时只有 status 和 publish_at。"""

    band: str
    platform: str
    key: str
    name: str
    rating: int | None = None
    url: str

    status: str
    available: bool = False
    publish_at: datetime | None = None

    hints: list[str] = Field(default_factory=list)
    body: str = ""
    code_available: bool = False
    code_publish_at: datetime | None = None
    code: dict[str, str] = Field(default_factory=dict)


class DayEditorials(BaseModel):
    """某一天的题解，按难度档排好。"""

    day: str
    problems: list[EditorialView] = Field(default_factory=list)


class RecentDay(BaseModel):
    """往日列表里的一条。"""

    day: str
    problem_count: int
    editorial_count: int


def _meta(session: Session, rows: list[DailyProblem]) -> dict[str, ProblemCache]:
    return editorials.problem_meta(session, rows)


def _parse_tags(cached: ProblemCache | None) -> list[str]:
    """标签存的是 JSON 字符串（各平台标签体系不同，存原文）。

    解析失败就当作没有标签：标签只是给生成器当参考，不值得为它报错。
    """
    if cached is None or not cached.tags_json:
        return []
    try:
        parsed = json.loads(cached.tags_json)
    except (ValueError, TypeError):
        return []
    return [str(tag) for tag in parsed] if isinstance(parsed, list) else []


def _view(session: Session, row: DailyProblem, cache: ProblemCache | None) -> EditorialView:
    editorial = editorials.get_for_problem(session, row.id or 0)
    base = {
        "band": row.band,
        "platform": row.platform,
        "key": row.key,
        "name": cache.name if cache else "",
        "rating": cache.rating if cache else None,
        "url": (cache.url if cache else "") or problem_url(row.platform, row.key),
    }

    if editorial is None:
        return EditorialView(**base, status=EditorialStatus.PENDING)

    return EditorialView(**base, **editorials.public_view(editorial))


@router.get("/daily/editorials", response_model=DayEditorials)
def get_day_editorials(
    day: str | None = Query(default=None, description="YYYY-MM-DD，默认今天"),
    session: Session = Depends(get_session),
    _user: User | None = Depends(get_optional_user),
) -> DayEditorials:
    """某一天的题解。

    未登录也能看——题解本身是学习资料，不该拦在登录后面。这里仍然注入
    ``get_optional_user`` 是为了和其他接口保持一致的会话处理（比如以后要
    记「谁看过」）。
    """
    target = daily_mod.workshop_today() if day is None else day
    try:
        datetime.strptime(target, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="日期格式应为 YYYY-MM-DD"
        ) from None

    rows = daily_mod.daily_for_day(session, target)
    cache = _meta(session, rows)
    return DayEditorials(
        day=target,
        problems=[_view(session, row, cache.get(row.key)) for row in rows],
    )


@router.get("/daily/editorials/recent", response_model=list[RecentDay])
def get_recent_days(
    limit: int = Query(default=30, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[RecentDay]:
    """往日的每日一题，从新到旧。

    只列真出过题的日期。按自然日往前推会得到一串空日期——题目是访问时
    才落库的，没人访问的那天没有记录。
    """
    today = daily_mod.workshop_today()
    days = editorials.recent_days(session, before=today, limit=limit)
    if not days:
        return []

    rows = session.exec(select(DailyProblem).where(DailyProblem.day.in_(days))).all()  # type: ignore[attr-defined]
    problem_count: dict[str, int] = {}
    for row in rows:
        problem_count[row.day] = problem_count.get(row.day, 0) + 1

    published = session.exec(
        select(Editorial).where(
            Editorial.day.in_(days),  # type: ignore[attr-defined]
            Editorial.published_at.is_not(None),  # type: ignore[union-attr]
        )
    ).all()
    editorial_count: dict[str, int] = {}
    for row in published:
        editorial_count[row.day] = editorial_count.get(row.day, 0) + 1

    return [
        RecentDay(
            day=day,
            problem_count=problem_count.get(day, 0),
            editorial_count=editorial_count.get(day, 0),
        )
        for day in days
    ]


# --------------------------------------------------------------------------- #
# 给生成器用的
# --------------------------------------------------------------------------- #

internal_router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_editorial_secret)],
)


class PendingProblem(BaseModel):
    """待生成题解的一道题，带上题面所需的信息。"""

    daily_problem_id: int
    band: str
    platform: str
    key: str
    name: str
    rating: int | None = None
    url: str
    tags: list[str] = Field(default_factory=list)
    #: 已经生成过没有。重跑时会覆盖。
    has_editorial: bool = False


class SubmitEditorial(BaseModel):
    """生成器提交的一篇题解。"""

    daily_problem_id: int
    #: 一句话提示，按由浅入深排列，每行一条。
    hints: str = ""
    #: 完整题解，Markdown + LaTeX。
    body: str
    #: 三个语言的参考代码。
    code: dict[str, str]
    model: str = ""


class SubmitResult(BaseModel):
    ok: bool
    id: int
    status: str


class FailReport(BaseModel):
    daily_problem_id: int
    error: str


@internal_router.get("/editorials/pending", response_model=list[PendingProblem])
def list_pending(
    day: str | None = Query(default=None, description="YYYY-MM-DD，默认今天"),
    session: Session = Depends(get_session),
) -> list[PendingProblem]:
    """某天还没生成题解的题，以及已有题解的题（供重跑）。

    生成器开工前先问这里，而不是自己按日期算题目——题目落库是访问时才
    发生的，进程直接算可能与站点看到的不一致。

    **这里会顺手把当天的题抽出来。** 抽题原本只在有人访问 ``/api/daily``
    时发生，而生成任务在凌晨 4:30 跑——那时候站点还没人访问，题目还没
    落库，于是它拿到一个空列表，什么都不做就退出了（2026-10-05 就这样
    漏了一整天）。

    生成器来问就说明它要用，在这里确保一下最省事。
    """
    target = daily_mod.workshop_today() if day is None else day
    # 幂等：当天已经有题就直接返回，不会重抽。
    rows = daily_mod.ensure_daily(session, day=target)
    if not rows:
        return []

    cache = _meta(session, rows)
    existing = editorials.get_for_day(session, target)

    out: list[PendingProblem] = []
    for row in rows:
        cached = cache.get(row.key)
        out.append(
            PendingProblem(
                daily_problem_id=row.id or 0,
                band=row.band,
                platform=row.platform,
                key=row.key,
                name=cached.name if cached else "",
                rating=cached.rating if cached else None,
                url=(cached.url if cached else "") or problem_url(row.platform, row.key),
                tags=_parse_tags(cached),
                has_editorial=row.key in existing,
            )
        )
    return out


@internal_router.post("/editorials", response_model=SubmitResult)
def submit_editorial(
    payload: SubmitEditorial,
    session: Session = Depends(get_session),
) -> SubmitResult:
    """写入或覆盖一篇题解。

    校验失败返回 400 并把原因写清楚，好让生成器知道该重跑还是该放弃。
    """
    problem = session.get(DailyProblem, payload.daily_problem_id)
    if problem is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="没有这道每日一题"
        )

    try:
        row = editorials.upsert(
            session,
            daily_problem_id=problem.id or 0,
            day=problem.day,
            platform=problem.platform,
            key=problem.key,
            hints=payload.hints,
            body=payload.body,
            code=payload.code,
            model=payload.model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return SubmitResult(ok=True, id=row.id or 0, status=row.status)


@internal_router.post("/editorials/fail", response_model=SubmitResult)
def report_failure(
    payload: FailReport,
    session: Session = Depends(get_session),
) -> SubmitResult:
    """报告一次生成失败。

    失败也落一行：前端靠它显示「题解生成失败」，否则那道题看起来像是
    「还没轮到生成」，两种情况该区分开。
    """
    problem = session.get(DailyProblem, payload.daily_problem_id)
    if problem is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="没有这道每日一题"
        )

    row = editorials.mark_failed(
        session,
        daily_problem_id=problem.id or 0,
        day=problem.day,
        platform=problem.platform,
        key=problem.key,
        error=payload.error,
    )
    return SubmitResult(ok=True, id=row.id or 0, status=row.status)


@internal_router.post("/editorials/publish-due")
def publish_due(session: Session = Depends(get_session)) -> dict:
    """把到点的草稿转成已公布。

    定时任务调。正常由 timer 驱动，这个接口是给手动补跑留的口子——
    比如某天服务停了，错过了 20:00 那一刻。
    """
    changed = editorials.publish_due(session, now=datetime.now(UTC))
    return {"changed": changed, "at": datetime.now(UTC).isoformat()}


@internal_router.get("/editorials/status")
def generator_status(
    day: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    """生成器的自检口：今天的题解齐了没有。"""
    target = daily_mod.workshop_today() if day is None else day
    rows = daily_mod.daily_for_day(session, target)
    existing = editorials.get_for_day(session, target)

    by_status: dict[str, int] = {}
    for row in existing.values():
        by_status[row.status] = by_status.get(row.status, 0) + 1

    return {
        "day": target,
        "problems": len(rows),
        "editorials": len(existing),
        "by_status": by_status,
        "pending": [row.key for row in rows if row.key not in existing],
    }


#: 管理员手动放行一篇草稿，不必等 20:00。
@router.post(
    "/daily/editorials/{editorial_id}/publish",
    dependencies=[Depends(require_admin)],
)
def publish_now(editorial_id: int, session: Session = Depends(get_session)) -> dict:
    """立刻公布一篇题解。管理员用。

    生成质量出问题、或者想提前放出来时可以手动推。
    """
    row = session.get(Editorial, editorial_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="没有这篇题解")
    if row.status in (EditorialStatus.FAILED, EditorialStatus.PENDING) or not row.body.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="题解尚未生成或正文为空，不能公布"
        )
    now = datetime.now(UTC)
    if row.published_at is None:
        row.published_at = now
    row.status = EditorialStatus.PUBLISHED
    row.updated_at = now
    session.add(row)
    session.commit()
    return {"ok": True, "id": editorial_id, "published_at": row.published_at}


__all__ = ["internal_router", "router"]
