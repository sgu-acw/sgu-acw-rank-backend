"""同步各平台的解题记录，算出每人每天的过题数。

两种数据形态，两种策略：

* **CF / AtCoder / 牛客** 的记录带精确时间戳。每次同步拉全量，按「每题
  最早一次通过」归到东八区的某一天，直接覆盖当天的计数。重复同步不会
  重复计数。
* **洛谷** 只给「累计通过哪些题」的集合，没有任何时间信息。于是把上一次
  抓到的集合存下来，这次多出来的题记为增量，归到「上次同步至今」这段
  时间里。第一次同步没有基线，只存快照不产生增量——否则会把一个人几年
  的积累全算到今天。

这个模块不依赖 FastAPI，可以单独跑：

    python -m app.sync
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, timedelta

from sqlalchemy import update
from sqlmodel import Session, select

from . import camps, daily, daytime, editorials, notifications
from . import posts as posts_mod
from .config import get_settings
from .db import get_engine, init_db, session_scope
from .models import (
    Account,
    DailyProblem,
    DailyProgress,
    Platform,
    SolvedSnapshot,
    SyncRun,
    User,
    utcnow,
)
from .platforms import FetchError, SolvedSet, fetch_profile, fetch_solved

logger = logging.getLogger("sgu_acw.sync")

#: 统计口径按东八区切日。CF 和 AtCoder 的时间戳是 UTC，成员说的「今天」
#: 是北京时间，直接按 UTC 分会把晚上的提交算到第二天。
#: 差分时最多回溯多久。如果两次同步隔了特别久（服务停了很久），把增量
#: 平摊到每天会让某一天虚高，不如只记最近这些天，其余忽略。
MAX_BACKFILL_DAYS = 7

#: 保留多少天的历史。榜单只看最近一段时间，多余的删掉控制库大小。
#: 1500 天约四年，覆盖成员入学到现在的全部过题史——CF/牛客全量拉
#: 本来就带来时间戳，写长窗口不增加任何平台请求。
RETENTION_DAYS = 1500


# 「一天」的定义在 daytime 里，每日一题那边也要用同一套。这里保留
# practice_day_of / current_practice_day 这两个名字，是因为路由层和测试
# 都按这些名字在调，改名没有好处。
_day_of = daytime.day_of_epoch
practice_day_of = daytime.day_of
current_practice_day = daytime.today

#: 东八区。测试和路由按这个名字在用，重新导出。
CST = daytime.CST


def _upsert_progress(
    db: Session,
    account: Account,
    date: str,
    count: int,
    *,
    replace: bool,
) -> None:
    """写入某账号某天的计数。

    ``replace=True`` 用于有时间戳的平台：那天的真实值就是 count，直接覆盖。
    ``replace=False`` 用于差分出来的增量：在已有值上累加，因为一次同步可能
    只是当天的一部分。
    """
    row = db.exec(
        select(DailyProgress).where(
            DailyProgress.account_id == account.id,
            DailyProgress.date == date,
        )
    ).first()

    if row is None:
        db.add(
            DailyProgress(
                account_id=account.id,
                platform=account.platform,
                date=date,
                solved_count=count,
                updated_at=utcnow(),
            )
        )
        return

    row.solved_count = count if replace else row.solved_count + count
    row.updated_at = utcnow()
    db.add(row)


def _apply_timestamped(
    db: Session,
    account: Account,
    solved: SolvedSet,
    *,
    since_day: str | None = None,
) -> int:
    """有时间戳的平台：按日聚合，覆盖式写入。

    只处理 ``since_day`` 之后的日子，避免每次同步都重写整段历史。

    返回的是**真正新增**的题数（与库里已有值相比），不是窗口内的总数。
    这个数决定账号算不算「活跃」，如果返回总数，一个不做题的人每次同步
    都会显得有新题，自适应调度就失效了。
    """
    per_day: Counter[str] = Counter()
    for epoch in solved.problems.values():
        if not epoch:
            continue
        day = _day_of(epoch)
        if since_day and day < since_day:
            continue
        per_day[day] += 1

    # 先读出这些天现有的值，算出差额再覆盖写入。
    existing: dict[str, int] = {}
    if per_day:
        rows = db.exec(
            select(DailyProgress).where(
                DailyProgress.account_id == account.id,
                DailyProgress.date.in_(list(per_day)),  # type: ignore[attr-defined]
            )
        ).all()
        existing = {row.date: row.solved_count for row in rows}

    added = 0
    for day, count in per_day.items():
        previous = existing.get(day, 0)
        if count > previous:
            added += count - previous
        _upsert_progress(db, account, day, count, replace=True)

    return added


def _apply_diff(
    db: Session,
    account: Account,
    solved: SolvedSet,
) -> int:
    """没有时间戳的平台：与上次快照比对，多出来的题记为增量。

    返回新增题数。首次同步只建立基线，返回 0。
    """
    snapshot = db.exec(
        select(SolvedSnapshot).where(SolvedSnapshot.account_id == account.id)
    ).first()

    current = set(solved.problems)
    previous = set(json.loads(snapshot.problems_json)) if snapshot else set()

    new_problems = current - previous
    is_first_sync = snapshot is None

    now = utcnow()
    if snapshot is None:
        db.add(
            SolvedSnapshot(
                account_id=account.id,
                problems_json=json.dumps(sorted(current), ensure_ascii=False),
                problem_count=len(current),
                fetched_at=now,
            )
        )
    else:
        snapshot.previous_fetched_at = snapshot.fetched_at
        snapshot.problems_json = json.dumps(sorted(current), ensure_ascii=False)
        snapshot.problem_count = len(current)
        snapshot.fetched_at = now
        db.add(snapshot)

    if is_first_sync or not new_problems:
        # 第一次没有基线，无法判断这些题是什么时候做的。
        return 0

    # 增量归属到「上次同步那天」到今天之间的日子。无法知道具体是哪天做
    # 的，全部记到上次同步那一天，这样总量不会丢，也不会把某天撑得虚高。
    anchor = date.fromisoformat(practice_day_of(snapshot.previous_fetched_at or now))
    today = date.fromisoformat(current_practice_day())
    oldest = today - timedelta(days=MAX_BACKFILL_DAYS)
    if anchor < oldest:
        anchor = oldest

    # 两次同步之间隔了几天，就把增量均摊到这些天，避免全压在同一天。
    span_days = max(1, (today - anchor).days + 1)
    per_day = len(new_problems) // span_days
    remainder = len(new_problems) % span_days

    day = anchor
    for index in range(span_days):
        count = per_day + (1 if index < remainder else 0)
        if count:
            _upsert_progress(
                db,
                account,
                day.strftime("%Y-%m-%d"),
                count,
                replace=False,
            )
        day += timedelta(days=1)

    return len(new_problems)


def _claim_sync_write(db: Session, account: Account, started_at) -> bool:
    """抓取后才取得写锁；较旧请求不能覆盖更新的同步结果。"""
    result = db.exec(
        update(Account).where(Account.id == account.id).values(identifier=Account.identifier)
    )
    if result.rowcount != 1:
        return False  # 抓取期间账号已解绑。
    db.refresh(account)
    return account.solved_fetched_at is None or account.solved_fetched_at <= started_at


def sync_account(db: Session, account: Account, *, force: bool = False) -> tuple[bool, str, int]:
    """同步一个账号的练习记录。返回 ``(是否成功, 说明, 新增题数)``。

    只动解题数据，不碰 rating——rating 变化慢，由 :func:`sync_rating` 每天
    查一次就够，两类数据分开调度。

    ``force=True`` 时忽略调度周期（成员手动刷新走这条）。
    """
    now = utcnow()
    settings = get_settings()

    force = force or camps.final_sync_due(db, account, now)
    if not force and not account.is_due(
        now=now,
        base=settings.sync_interval_base_minutes * 60,
        cap=settings.sync_interval_cap_minutes * 60,
    ):
        return True, "未到同步时间", 0

    try:
        solved = fetch_solved(account.platform, account.identifier)
    except FetchError as exc:
        if not _claim_sync_write(db, account, now):
            return True, "同步结果已更新，忽略旧请求", 0
        # 失败也要记时间，否则下次唤醒会立刻重试，一个持续失败的账号会
        # 每五分钟撞一次墙。但重试间隔要退避：失败次数越多等得越久。
        account.solved_fetched_at = now
        if exc.kind:
            # 配置类错误（隐私保护、未实名）：账号在但当前不可统计。
            # 记成功 + 打标记，不进入失败退避，也不刷屏站内信——本人
            # 或管理员在账号列表能看到标记。
            account.sync_flag = exc.kind
            account.consecutive_failures = 0
            account.last_error = str(exc)[:500]
            db.add(account)
            return True, str(exc), 0

        account.consecutive_failures += 1
        account.last_error = str(exc)[:500]
        db.add(account)
        owner = db.get(User, account.user_id)
        owner_name = owner.display_name if owner else f"#{account.user_id}"
        notifications.report(
            db,
            f"sync:{account.id}",
            "平台成绩同步失败",
            f"{owner_name} 的 {Platform.LABELS.get(account.platform, account.platform)} "
            f"账号 {account.identifier} 同步失败（{str(exc)[:80]}），已安排重试。",
            "/admin",
        )
        return False, str(exc), 0

    if not _claim_sync_write(db, account, now):
        return True, "同步结果已更新，忽略旧请求", 0
    if account.platform == Platform.LUOGU:
        # 只有洛谷没有时间戳，走差分。
        added = _apply_diff(db, account, solved)
        detail = f"累计 {len(solved.problems)} 题，新增 {added}"
    else:
        # 其余平台（CF / AtCoder / 牛客）都有时间戳。只重算最近一段时间，
        # 够覆盖排行榜展示的窗口，也不必每次重写全部历史。
        since = (
            date.fromisoformat(current_practice_day()) - timedelta(days=RETENTION_DAYS)
        ).strftime("%Y-%m-%d")
        added = _apply_timestamped(db, account, solved, since_day=since)
        detail = f"累计 {len(solved.problems)} 题"
        _save_snapshot(db, account, solved)

    account.solved_fetched_at = now
    account.last_error = None
    account.consecutive_failures = 0
    account.sync_flag = None
    # 只有真的拓到新题才算「活跃」。否则一个不做题的人会因为反复查看而
    # 一直停留在高频档位。
    if added > 0:
        account.last_active_at = now
    db.add(account)

    _record_daily_checkin(db, account, solved)
    camps.record_final_sync(db, account, solved, now)
    notifications.resolve(db, f"sync:{account.id}")

    return True, detail, added


def _record_daily_checkin(db: Session, account: Account, solved) -> None:
    """看看这个人今天有没有做对每日一题，有就记一次签到。

    在同步末尾做，因为要拿到的正是刚抓回来的已过题目。放在这里还有个
    好处：成员一同步就自动签到，不用额外点一下按钮。

    只比对**今天**的题。昨天的题今天才做出来不算今天的签到——「每日」
    一题的意思就是当天的。

    没有每日一题（还没抽、或者题库没准备好）时静默跳过，不能因为这个小
    功能让整个同步失败。
    """
    from .daily import record_solves, workshop_today

    day = workshop_today()
    today_problems = db.exec(select(DailyProblem).where(DailyProblem.day == day)).all()
    if not today_problems:
        return

    wanted = {(row.platform, row.key) for row in today_problems}
    hit = wanted & {(account.platform, str(key)) for key in solved.problems}
    if not hit:
        return

    record_solves(
        db,
        user_id=account.user_id,
        account_id=account.id,
        day=day,
        solved_keys=hit,
    )


def _save_snapshot(db: Session, account: Account, solved) -> None:
    """存一份已过题目的快照，便于排查抓取是否正常。"""
    snapshot = db.exec(
        select(SolvedSnapshot).where(SolvedSnapshot.account_id == account.id)
    ).first()
    payload = json.dumps(sorted(solved.problems), ensure_ascii=False)
    if snapshot is None:
        db.add(
            SolvedSnapshot(
                account_id=account.id,
                problems_json=payload,
                problem_count=len(solved.problems),
                fetched_at=utcnow(),
            )
        )
    else:
        snapshot.problems_json = payload
        snapshot.problem_count = len(solved.problems)
        snapshot.fetched_at = utcnow()
        db.add(snapshot)


def sync_rating(db: Session, account: Account, *, force: bool = False) -> tuple[bool, str]:
    """同步账号的 rating。返回 ``(是否成功, 说明)``。

    rating 只在打完比赛后变，变化很慢，所以默认每天查一次。与过题数的
    调度分开，两者周期不同。
    """
    now = utcnow()
    settings = get_settings()

    if not force and account.rating_fetched_at is not None:
        elapsed = (now - account.rating_fetched_at).total_seconds()
        if elapsed < settings.rating_interval_minutes * 60:
            return True, "未到更新时间"

    try:
        profile = fetch_profile(account.platform, account.identifier)
    except FetchError as exc:
        # rating 抓不到不算严重：过题数才是榜单主体。只记下来。
        account.rating_failures += 1
        account.last_error = str(exc)[:500]
        account.rating_fetched_at = now
        db.add(account)
        return False, str(exc)

    account.rating = profile.rating
    account.max_rating = profile.max_rating
    if profile.display_name:
        account.display_name = profile.display_name
    account.rating_fetched_at = now
    account.rating_failures = 0
    db.add(account)
    return True, f"rating={profile.rating}"


def prune_old_progress(db: Session) -> int:
    """删掉超过保留期的记录。返回删除行数。"""
    cutoff = (date.fromisoformat(current_practice_day()) - timedelta(days=RETENTION_DAYS)).strftime(
        "%Y-%m-%d"
    )
    rows = db.exec(select(DailyProgress).where(DailyProgress.date < cutoff)).all()
    for row in rows:
        db.delete(row)
    return len(rows)


def run_sync(*, platforms: list[str] | None = None, force: bool = False) -> dict:
    """同步所有已验证账号。返回这次同步的摘要。

    ``force=True`` 忽略调度周期，把所有账号都抓一遍（成员手动刷新、管理员
    排查时用）。

    返回普通 dict 而不是 ORM 对象：session 关掉之后 ORM 实例就 detached 了，
    调用方一读属性就报 DetachedInstanceError。
    """
    init_db()

    # 持久化结算任务，无时间窗口限制；停机恢复后继续未完成的账号。
    with session_scope() as db:
        camps.prepare_finalizations(db)
        statement = select(Account).where(Account.verified_at.is_not(None))  # type: ignore[union-attr]
        if platforms:
            statement = statement.where(Account.platform.in_(platforms))  # type: ignore[attr-defined]
        account_ids = [a.id for a in db.exec(statement).all()]

    errors: list[str] = []
    ok = 0
    total = 0
    fetched = 0

    for account_id in account_ids:
        # 每个账号一个独立事务。抓取是网络操作，一个账号要 0.3～5 秒；如果
        # 把整个循环包在一个事务里，写锁会被持有几分钟，期间任何登录、绑定、
        # 手动刷新都会被卡住（实测能卡 3.5 秒以上）。拆开之后锁只在自己那
        # 一小段写入期间持有，毫秒级。
        #
        # 代价是同步不再是原子的，但这本来就不需要：一个账号失败不该回滚
        # 别人的结果。
        with session_scope() as db:
            account = db.get(Account, account_id)
            if account is None:
                continue

            success, detail, added = sync_account(db, account, force=force)
            if detail == "未到同步时间":
                # 没到点就不算「抓过」，也不记日志。这是绝大多数情况：
                # 每五分钟唤醒一次，而大多数账号的间隔比这长得多。
                sync_rating(db, account, force=force)
                continue

            fetched += 1
            if success:
                ok += 1
                total += added
                logger.info("synced %s/%s: %s", account.platform, account.identifier, detail)
            else:
                errors.append(f"{account.platform}/{account.identifier}: {detail}")
                logger.warning(
                    "sync failed %s/%s: %s", account.platform, account.identifier, detail
                )

            # rating 与过题数分开调度，各有各的周期。
            sync_rating(db, account, force=force)

    with session_scope() as db:
        camps.finish_finalizations(db)

    # 只有真的抓了账号才记一行。每五分钟唤醒一次的话，每次都记会攒出十万
    # 行／年的噪音，而绝大多数唤醒是空跑。
    if fetched:
        with session_scope() as db:
            run = SyncRun(started_at=utcnow())
            run.finished_at = utcnow()
            run.account_count = fetched
            run.ok_count = ok
            run.error_count = len(errors)
            run.errors_json = json.dumps(errors, ensure_ascii=False)
            db.add(run)

            pruned = prune_old_progress(db)
            if pruned:
                logger.info("pruned %d old progress rows", pruned)

    logger.info(
        "sync done: %d fetched, %d ok, %d errors, %d new problems",
        fetched,
        ok,
        len(errors),
        total,
    )

    # 换天之后把当天的题抽出来。
    #
    # 抽题原本只在有人访问 /api/daily 时发生，于是凌晨 4:30 的生成任务
    # 拿不到题目——那时候站点还没人访问。2026-10-05 就这样漏了一整天。
    #
    # 搭在同步任务上而不是单独一个 timer：同步每 5 分钟跑一次，换天后的
    # 第一次唤醒就会抽，不用再多一个 unit。ensure_daily 是幂等的，
    # 当天已经抽过就直接返回。
    try:
        with session_scope() as db:
            drawn = daily.ensure_daily(db)
            drawn_keys = [row.key for row in drawn]
        logger.info("daily problems ready: %s", ", ".join(drawn_keys))
        with session_scope() as db:
            notifications.resolve(db, "daily-draw")
    except Exception:  # noqa: BLE001 - 抽题失败不能拖垮同步
        logger.exception("drawing daily problems failed")
        with session_scope() as db:
            notifications.report(
                db,
                "daily-draw",
                "每日一题抽取失败",
                "自动抽题失败，请检查题库和同步服务日志。",
                "/editorials/admin",
            )

    # 公告到点撤销。和题解公布同一个小动作：过点把开关关掉，免得管理页
    # 一直显示「取消公告」。
    try:
        with session_scope() as db:
            cleared = posts_mod.sweep_expired_popups(db)
        if cleared:
            logger.info("swept %d expired popup(s)", cleared)
    except Exception:  # noqa: BLE001 - 撤销失败不能拖垮同步
        logger.exception("sweeping expired popups failed")

    # 题解到点就公布。搭在同步任务上而不是单独一个 timer：公布只是把
    # 两个时间戳填上，几毫秒的事，而同步每 5 分钟就跑一次，足够准。
    #
    # 到点判据是「当前时间 >= 该公布的时刻」，所以错过几小时也不会漏掉，
    # 下一次唤醒会补上。
    try:
        with session_scope() as db:
            changed = editorials.publish_due(db)
        if changed:
            logger.info("published %d editorial timestamp(s)", changed)
        with session_scope() as db:
            notifications.resolve(db, "editorial-publish")
    except Exception:  # noqa: BLE001 - 公布失败不能拖垮同步
        logger.exception("publishing due editorials failed")
        with session_scope() as db:
            notifications.report(
                db,
                "editorial-publish",
                "题解自动发布失败",
                "题解发布任务失败，请检查服务日志。",
                "/editorials/admin",
            )

    return {
        "account_count": fetched,
        "ok_count": ok,
        "error_count": len(errors),
        "new_problems": total,
        "errors": errors,
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    result = run_sync()
    print(
        f"同步完成：{result['account_count']} 个账号，"
        f"成功 {result['ok_count']}，失败 {result['error_count']}，"
        f"新增 {result['new_problems']} 题"
    )
    for line in result["errors"]:
        print(f"  ! {line}")


if __name__ == "__main__":
    main()


__all__ = ["run_sync", "sync_account", "get_engine"]
