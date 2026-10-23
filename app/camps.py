"""集训状态和成绩：预告 → 进行中 → 最终同步 → 封榜。

历史完成计分。有时间戳的平台过滤截止后的通过；洛谷采用截止后首次成功
抓取的清单。每个账号的最终结果单独持久化，失败重试不能覆盖别人的结果。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlmodel import Session, func, select

from . import notifications
from .daytime import CST
from .learning_helpers import learn_done_by_user
from .models import (
    Account,
    Camp,
    CampAccountResult,
    CampFinalization,
    CampProblem,
    CampRegistration,
    Platform,
    ProblemCache,
    SolvedSnapshot,
    User,
)
from .problems import normalize_key, problem_url


class CampError(Exception):
    """带有用户提示的集训业务错误。"""


def lock_camp(session: Session, camp_id: int) -> Camp | None:
    """先取得 SQLite 写锁，再读取状态，串行化名单/题目变更与封榜准备。

    不在持锁期间做网络抓取；调用方在同一个事务内校验并提交。
    """
    session.exec(update(Camp).where(Camp.id == camp_id).values(name=Camp.name))
    return session.get(Camp, camp_id, populate_existing=True)


def platforms_of(session: Session, camp_id: int) -> set[str]:
    """集训涉及的可同步平台；本地练习题不算报名前置，也不参与同步。"""
    return set(
        session.exec(
            select(CampProblem.platform)
            .where(
                CampProblem.camp_id == camp_id,
                CampProblem.platform != Platform.LEARN,
            )
            .distinct()
        ).all()
    )


def verified_platforms(session: Session, user_id: int) -> set[str]:
    return set(
        session.exec(
            select(Account.platform)
            .where(
                Account.user_id == user_id,
                Account.verified_at.is_not(None),
            )
            .distinct()
        ).all()
    )


def register_open_from(camp: Camp) -> datetime | None:
    """报名开放的生效时间。绝对值优先，否则按开始时间回推。"""
    if camp.register_open_at is not None:
        return camp.register_open_at
    if camp.register_open_days is not None:
        return camp.start_at - timedelta(days=camp.register_open_days)
    return None


def visible_from(camp: Camp) -> datetime | None:
    """开始显示的生效时间。绝对值优先，否则按开始时间回推。"""
    if camp.visible_at is not None:
        return camp.visible_at
    if camp.visible_days is not None:
        return camp.start_at - timedelta(days=camp.visible_days)
    return None


def can_register(session: Session, *, camp: Camp, user_id: int) -> tuple[bool, str]:
    now = datetime.now(UTC)
    if now >= camp.end_at:
        return False, "已截止"
    opens = register_open_from(camp)
    if opens is not None and now < opens:
        return False, f"报名还没开放，{opens.astimezone(CST).strftime('%m月%d日 %H:%M')} 开始"
    needed = platforms_of(session, camp.id or 0)
    if not needed:
        # 全是本地练习题的集训不需要绑定平台账号，只要有题就能报名。
        has_problems = session.exec(
            select(CampProblem.id).where(CampProblem.camp_id == camp.id).limit(1)
        ).first()
        if has_problems is None:
            return False, "这份集训还没出题"
        return True, ""
    missing = needed - verified_platforms(session, user_id)
    labels = "、".join(Platform.LABELS.get(p, p) for p in sorted(missing))
    has_learning = (
        session.exec(
            select(CampProblem.id)
            .where(CampProblem.camp_id == camp.id, CampProblem.platform == Platform.LEARN)
            .limit(1)
        ).first()
        is not None
    )
    if missing == needed and not has_learning:
        return False, f"请先绑定并验证至少一个账号：{labels}"
    return True, f"尚未验证 {labels} 账号，这些平台的成绩暂不能同步" if missing else ""


def boost_accounts(session: Session, camp: Camp, user_id: int | None = None) -> None:
    """报名、加题或延长时间后统一调整同步；不清空已经成功抓到的快照。"""
    if datetime.now(UTC) >= camp.end_at:
        return
    ids = (
        [user_id]
        if user_id is not None
        else session.exec(
            select(
                CampRegistration.user_id,
            ).where(CampRegistration.camp_id == camp.id)
        ).all()
    )
    accounts = session.exec(
        select(Account).where(
            Account.user_id.in_(ids),
            Account.platform.in_(platforms_of(session, camp.id or 0)),
            Account.verified_at.is_not(None),
        )
    ).all()
    for acc in accounts:
        acc.solved_fetched_at = None
        acc.sync_boost_until = max(acc.sync_boost_until or camp.end_at, camp.end_at)
        session.add(acc)


def register(
    session: Session, *, camp: Camp, user_id: int, admin: bool = False
) -> CampRegistration:
    camp = lock_camp(session, camp.id or 0)
    if camp is None:
        raise CampError("集训不存在")
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise CampError("成员不存在或已停用")
    existing = session.exec(
        select(CampRegistration).where(
            CampRegistration.camp_id == camp.id, CampRegistration.user_id == user_id
        )
    ).first()
    if existing is not None:
        # 成功请求的重试只返回现有报名，不再触发账号同步。
        session.commit()
        return existing
    now = datetime.now(UTC)
    if now >= camp.end_at:
        raise CampError("集训已截止，报名名单已锁定")
    visible = visible_from(camp)
    if not admin and visible is not None and now < visible:
        raise CampError("这份集训还没有公开")
    opens = register_open_from(camp)
    if not admin and opens is not None and now < opens:
        raise CampError(f"报名还没开放，{opens.astimezone(CST).strftime('%m月%d日 %H:%M')} 开始")
    if not admin:
        ok, reason = can_register(session, camp=camp, user_id=user_id)
        if not ok:
            raise CampError(reason)
    row = CampRegistration(camp_id=camp.id or 0, user_id=user_id)
    session.add(row)
    session.flush()
    boost_accounts(session, camp, user_id)
    session.commit()
    session.refresh(row)
    return row


def unregister(session: Session, *, camp: Camp, user_id: int) -> None:
    camp = lock_camp(session, camp.id or 0)
    if camp is None:
        raise CampError("集训不存在")
    row = session.exec(
        select(CampRegistration).where(
            CampRegistration.camp_id == camp.id,
            CampRegistration.user_id == user_id,
        )
    ).first()
    if row is None:
        session.commit()
        return
    if datetime.now(UTC) >= camp.start_at:
        raise CampError("集训已开始，请联系管理员调整报名")
    session.delete(row)
    session.commit()


def is_registered(session: Session, *, camp_id: int, user_id: int) -> bool:
    return (
        session.exec(
            select(CampRegistration.id).where(
                CampRegistration.camp_id == camp_id,
                CampRegistration.user_id == user_id,
            )
        ).first()
        is not None
    )


def leaderboard(session: Session, camp: Camp) -> list[tuple[User, int, dict[str, bool]]]:
    """实时榜单，批量读账号和快照；不随人数增加查询次数。"""
    problems = session.exec(
        select(CampProblem)
        .where(CampProblem.camp_id == camp.id)
        .order_by(CampProblem.position, CampProblem.id)
    ).all()
    if not problems:
        return []
    users = session.exec(
        select(User)
        .join(CampRegistration)
        .where(
            CampRegistration.camp_id == camp.id,
        )
    ).all()
    snapshots = session.exec(
        select(Account.user_id, Account.platform, SolvedSnapshot.problems_json)
        .join(SolvedSnapshot, SolvedSnapshot.account_id == Account.id)
        .where(Account.user_id.in_([u.id for u in users]), Account.verified_at.is_not(None))
    ).all()
    solved: dict[int, set[tuple[str, str]]] = {}
    for uid, platform, payload in snapshots:
        solved.setdefault(uid, set()).update(
            (platform, normalize_key(platform, str(k))) for k in json.loads(payload)
        )
    # 题库题与其他平台使用同一截止时间，历史通过计分，截止后的提交不计分。
    learn_done = (
        learn_done_by_user(session, [u.id for u in users], through=camp.end_at)
        if any(p.platform == Platform.LEARN for p in problems)
        else {}
    )
    rows = []
    for user in users:
        done = solved.get(user.id, set())
        learn = learn_done.get(user.id, set())
        per_problem = {
            f"{p.platform}:{p.key}": (
                str(p.key) in learn if p.platform == Platform.LEARN else (p.platform, p.key) in done
            )
            for p in problems
        }
        rows.append((user, sum(per_problem.values()), per_problem))
    rows.sort(key=lambda row: (-row[1], row[0].student_id, row[0].id))
    return rows


def standings(session: Session, camp: Camp) -> list[dict]:
    result = session.get(CampFinalization, camp.id)
    if result and result.finalized_at:
        return json.loads(result.standings_json)
    # 标准竞赛名次：同分同名次，后面的位次跳过（1、2、2、4）。
    # 行内排序仍然稳定——同分按学号，名次一样时谁先谁后不影响阅读。
    out = []
    rank = 0
    prev = None
    for i, (u, count, per_problem) in enumerate(leaderboard(session, camp)):
        if prev is None or count < prev:
            rank = i + 1
        prev = count
        out.append(
            {
                "rank": rank,
                "user_id": u.id,
                "display_name": u.display_name,
                "student_id": u.student_id,
                "enroll_year": u.enroll_year,
                "class_label": u.class_label,
                "college_name": u.college_name,
                "major_name": u.major_name,
                "solved_count": count,
                "per_problem": per_problem,
            }
        )
    return out


def result_status(session: Session, camp: Camp) -> dict:
    result = session.get(CampFinalization, camp.id)
    now = datetime.now(UTC)
    phase = (
        ("finalized" if result and result.finalized_at else "finalizing")
        if now >= camp.end_at
        else ("running" if now >= camp.start_at else "scheduled")
    )
    pending = (
        session.exec(
            select(func.count())
            .select_from(CampAccountResult)
            .where(
                CampAccountResult.camp_id == camp.id,
                CampAccountResult.completed_at.is_(None),
            )
        ).one()
        if result and not result.finalized_at
        else 0
    )
    return {
        "phase": phase,
        "finalized_at": result.finalized_at if result else None,
        "pending_accounts": pending,
    }


def prepare_finalizations(session: Session) -> None:
    """持久化待结算账号；停机超过一个 tick 也不漏结算。"""
    now = datetime.now(UTC)
    existing = select(CampFinalization.camp_id)
    for camp in session.exec(
        select(Camp).where(Camp.end_at <= now, Camp.id.not_in(existing))
    ).all():
        camp = lock_camp(session, camp.id or 0)
        if camp is None or camp.end_at > now or session.get(CampFinalization, camp.id):
            continue
        users = session.exec(
            select(User)
            .join(CampRegistration)
            .where(
                CampRegistration.camp_id == camp.id,
            )
        ).all()
        problems = session.exec(
            select(CampProblem)
            .where(CampProblem.camp_id == camp.id)
            .order_by(CampProblem.position, CampProblem.id)
        ).all()
        # 冻结全部题目；仅外部平台账号需要最终同步。
        accounts = session.exec(
            select(Account).where(
                Account.user_id.in_([u.id for u in users]),
                Account.platform.in_(
                    {p.platform for p in problems if p.platform != Platform.LEARN}
                ),
                Account.verified_at.is_not(None),
            )
        ).all()
        participants = [
            {
                "user_id": u.id,
                "display_name": u.display_name,
                "student_id": u.student_id,
                "enroll_year": u.enroll_year,
                "class_label": u.class_label,
                "college_name": u.college_name,
                "major_name": u.major_name,
                "luogu": next(
                    (a.identifier for a in accounts if a.user_id == u.id and a.platform == "luogu"),
                    "",
                ),
            }
            for u in users
        ]
        session.add(
            CampFinalization(
                camp_id=camp.id,
                participants_json=json.dumps(participants),
                problems_json=json.dumps([f"{p.platform}:{p.key}" for p in problems]),
            )
        )
        for acc in accounts:
            session.add(
                CampAccountResult(
                    camp_id=camp.id, account_id=acc.id, user_id=acc.user_id, platform=acc.platform
                )
            )
            acc.solved_fetched_at = None
            session.add(acc)
    session.flush()


def final_sync_due(session: Session, account: Account, now: datetime) -> bool:
    pending = session.exec(
        select(CampAccountResult.id).where(
            CampAccountResult.account_id == account.id,
            CampAccountResult.completed_at.is_(None),
        )
    ).first()
    if pending is None:
        return False
    # 持续失败时最多一小时重试一次，不能因普通活跃度退避到七天。
    delay = min(3600, 300 * 2 ** min(account.consecutive_failures, 4))
    return (
        account.solved_fetched_at is None
        or (now - account.solved_fetched_at).total_seconds() >= delay
    )


def record_final_sync(session: Session, account: Account, solved, fetched_at: datetime) -> None:
    for row in session.exec(
        select(CampAccountResult).where(
            CampAccountResult.account_id == account.id,
            CampAccountResult.completed_at.is_(None),
        )
    ).all():
        camp = session.get(Camp, row.camp_id)
        if camp is None or fetched_at < camp.end_at:
            continue
        payload = json.dumps(
            [
                normalize_key(account.platform, str(key))
                for key, timestamp in solved.problems.items()
                if not timestamp or timestamp <= camp.end_at.timestamp()
            ]
        )
        # 并发同步可能已读到同一条 pending；仅第一次成功写入能占有最终结果。
        session.exec(
            update(CampAccountResult)
            .where(CampAccountResult.id == row.id, CampAccountResult.completed_at.is_(None))
            .values(solved_json=payload, completed_at=fetched_at)
        )


def finish_finalizations(session: Session) -> None:
    now = datetime.now(UTC)
    for result in session.exec(
        select(CampFinalization).where(
            CampFinalization.finalized_at.is_(None),
        )
    ).all():
        accounts = session.exec(
            select(CampAccountResult).where(
                CampAccountResult.camp_id == result.camp_id,
            )
        ).all()
        pending = sum(a.completed_at is None for a in accounts)
        key = f"camp-finalization:{result.camp_id}"
        if pending:
            if now - result.started_at >= timedelta(minutes=15):
                camp = session.get(Camp, result.camp_id)
                camp_name = camp.name if camp else f"#{result.camp_id}"
                notifications.report(
                    session,
                    key,
                    "集训封榜等待同步",
                    f"「{camp_name}」还有 {pending} 个账号未完成最终同步；成绩尚未封榜。",
                    f"/camps/admin?id={result.camp_id}",
                )
            continue
        solved: dict[int, set[str]] = {}
        for acc in accounts:
            solved.setdefault(acc.user_id, set()).update(
                f"{acc.platform}:{p}" for p in json.loads(acc.solved_json)
            )
        rows = json.loads(result.participants_json)
        keys = json.loads(result.problems_json)
        # 兼容升级前已进入最终同步、尚未封榜的任务；截止后题单已锁定。
        # 只在首次完成结算时补齐遗漏的题库列，已封榜快照不经过此分支。
        for problem in session.exec(
            select(CampProblem)
            .where(CampProblem.camp_id == result.camp_id, CampProblem.platform == Platform.LEARN)
            .order_by(CampProblem.position, CampProblem.id)
        ).all():
            problem_key = f"{Platform.LEARN}:{problem.key}"
            if problem_key not in keys:
                keys.append(problem_key)
        if any(key.startswith(f"{Platform.LEARN}:") for key in keys):
            camp = session.get(Camp, result.camp_id)
            if camp is None:
                continue
            learning = learn_done_by_user(
                session, [row["user_id"] for row in rows], through=camp.end_at
            )
            for uid, problem_ids in learning.items():
                solved.setdefault(uid, set()).update(
                    f"{Platform.LEARN}:{problem_id}" for problem_id in problem_ids
                )
        for row in rows:
            row["per_problem"] = {key: key in solved.get(row["user_id"], set()) for key in keys}
            row["solved_count"] = sum(row["per_problem"].values())
        rows.sort(key=lambda row: (-row["solved_count"], row["student_id"], row["user_id"]))
        rank = 0
        prev = None
        for i, row in enumerate(rows):
            if prev is None or row["solved_count"] < prev:
                rank = i + 1
            prev = row["solved_count"]
            row["rank"] = rank
        # 以条件写入保护最终快照，重复结算不会重新生成封榜时间。
        session.exec(
            update(CampFinalization)
            .where(
                CampFinalization.camp_id == result.camp_id,
                CampFinalization.finalized_at.is_(None),
            )
            .values(
                problems_json=json.dumps(keys), standings_json=json.dumps(rows), finalized_at=now
            )
        )
        notifications.resolve(session, key)


def problem_meta(
    session: Session, problems: list[CampProblem]
) -> dict[tuple[str, str], ProblemCache]:
    keys = {(p.platform, p.key) for p in problems}
    if not keys:
        return {}
    return {
        (r.platform, r.key): r
        for r in session.exec(
            select(ProblemCache).where(
                ProblemCache.key.in_({k for _, k in keys}),
            )
        ).all()
        if (r.platform, r.key) in keys
    }


def detail_for_user(session: Session, camp: Camp, user: User | None) -> dict:
    now = datetime.now(UTC)
    registered = user is not None and is_registered(session, camp_id=camp.id, user_id=user.id)
    problems = session.exec(
        select(CampProblem)
        .where(CampProblem.camp_id == camp.id)
        .order_by(CampProblem.position, CampProblem.id)
    ).all()
    ok, reason = (
        can_register(session, camp=camp, user_id=user.id)
        if user and not registered
        else (False, "")
    )
    result = {
        "id": camp.id,
        "name": camp.name,
        "description": camp.description,
        "start_at": camp.start_at,
        "end_at": camp.end_at,
        "started": now >= camp.start_at,
        "ended": now >= camp.end_at,
        "register_open": register_open_from(camp),
        "visible_from": visible_from(camp),
        "registered": registered,
        "registration_count": session.exec(
            select(func.count())
            .select_from(CampRegistration)
            .where(CampRegistration.camp_id == camp.id)
        ).one(),
        "problem_count": len(problems),
        # 只列需要绑定账号的平台；本地练习不需要账号，不放进提示里。
        "platforms": sorted({p.platform for p in problems if p.platform != Platform.LEARN}),
        "can_register": ok,
        "register_reason": reason,
        "can_unregister": registered and now < camp.start_at,
        "problems": [],
        "leaderboard": [],
        **result_status(session, camp),
    }
    mine = {}
    if registered and now >= camp.start_at:
        board = standings(session, camp)
        mine = next((r["per_problem"] for r in board if r["user_id"] == user.id), {})
        result["leaderboard"] = board
    # 截止即公开题单，最终同步/封榜不阻塞查看；成绩仍仅供已报名成员查看。
    if now >= camp.end_at or (registered and now >= camp.start_at):
        meta = problem_meta(session, problems)
        for p in problems:
            row = meta.get((p.platform, p.key))
            result["problems"].append(
                {
                    "platform": p.platform,
                    "key": p.key,
                    "name": row.name if row else "",
                    "rating": row.rating if row else None,
                    "url": problem_url(p.platform, p.key),
                    "done": mine.get(f"{p.platform}:{p.key}", False),
                }
            )
    return result
