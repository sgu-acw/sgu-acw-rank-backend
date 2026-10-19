"""题单的读取与完成度计算。

完成度 = 完成的题数 / 总题数。判断「完成」的办法是把题单里的题号与这个人
已过的题号做集合比对——两边都用平台原生写法，所以不需要任何格式转换。

一个人可能绑了多个账号（主号加小号），任一账号过了就算过了。这与练习量榜
的口径一致：那里也是把同一平台的主号小号合并统计的。

未登录时不显示进度：谁能过哪些题是个人数据，不该公开给所有人看。
"""

from __future__ import annotations

import json

from sqlmodel import Session, select

from .models import (
    Account,
    DailyProgress,
    Platform,
    ProblemCache,
    ProblemList,
    ProblemListItem,
    SolvedSnapshot,
)
from .problems import problem_url
from .schemas import (
    ProblemEntry,
    ProblemListDetail,
    ProblemListPublic,
    ProblemListRankEntry,
)

#: 题单里最多放多少题。没有硬上限的话，有人一次导入几千题，列表页会卡死。
MAX_PROBLEMS_PER_LIST = 500


def solved_keys(db: Session, user_id: int) -> set[tuple[str, str]]:
    """某人已过的所有题，返回 (平台, 题号) 的集合。

    同一平台的主号小号取并集——这与练习量榜的口径一致。
    """
    accounts = db.exec(select(Account).where(Account.user_id == user_id)).all()
    if not accounts:
        return set()

    account_ids = [a.id for a in accounts]
    platform_of = {a.id: a.platform for a in accounts}

    snapshots = db.exec(
        select(SolvedSnapshot).where(SolvedSnapshot.account_id.in_(account_ids))  # type: ignore[attr-defined]
    ).all()

    out: set[tuple[str, str]] = set()
    for snapshot in snapshots:
        platform = platform_of.get(snapshot.account_id)
        if not platform:
            continue
        try:
            keys = json.loads(snapshot.problems_json)
        except (TypeError, ValueError):
            continue
        for key in keys:
            out.add((platform, str(key)))
    return out


def cached_problems(db: Session, platform: str, keys: list[str]) -> dict[str, ProblemCache]:
    """一次取出若干题的缓存，避免逐题查库。"""
    if not keys:
        return {}
    rows = db.exec(
        select(ProblemCache).where(
            ProblemCache.platform == platform,
            ProblemCache.key.in_(keys),  # type: ignore[attr-defined]
        )
    ).all()
    return {row.key: row for row in rows}


def to_entry(item: ProblemListItem, cache: ProblemCache | None, done: bool | None) -> ProblemEntry:
    """把一条题单条目转成接口返回的样子。"""
    entry = ProblemEntry(
        id=item.id or 0,
        platform=item.platform,
        key=item.key,
        position=item.position,
        note=item.note,
        done=done,
    )
    # 题面地址一律现算，不用缓存里的那份。
    #
    # 地址完全由「平台 + 题号」决定，没有抓取才能知道的成分，所以现算
    # 总是对的。而缓存里的那份会过期：修过一次牛客的地址拼接（原来把
    # ``nowcoder`` 当成来源，全部落到搜索页），但缓存里存着的是修复前
    # 拼出来的坏地址，不现算的话用户点的还是坏链接。
    entry.url = problem_url(item.platform, item.key)

    if cache is not None:
        entry.name = cache.name
        entry.rating = cache.rating
        entry.tags = json.loads(cache.tags_json or "[]")
        # 缓存里的地址只在现算不出结果时兜底。
        if not entry.url:
            entry.url = cache.url
        entry.missing_reason = cache.missing_reason
    else:
        entry.missing_reason = "题目信息还没抓取"
    return entry


def list_problems(
    db: Session,
    problem_list: ProblemList,
    *,
    viewer_id: int | None,
) -> list[ProblemEntry]:
    """题单里的题目，按 position 排序，附上查看者的完成状态。"""
    items = db.exec(
        select(ProblemListItem)
        .where(ProblemListItem.list_id == problem_list.id)
        .order_by(ProblemListItem.position, ProblemListItem.id)  # type: ignore[attr-defined]
    ).all()

    by_platform: dict[str, list[str]] = {}
    for item in items:
        by_platform.setdefault(item.platform, []).append(item.key)

    caches: dict[tuple[str, str], ProblemCache] = {}
    for platform, keys in by_platform.items():
        for key, row in cached_problems(db, platform, keys).items():
            caches[(platform, key)] = row

    done_set = solved_keys(db, viewer_id) if viewer_id is not None else None
    local_done: set[str] = set()
    if viewer_id is not None and any(i.platform == Platform.LEARN for i in items):
        from .learning_helpers import learn_done_keys

        # 本人客户端 AC 标记只影响自己的视图，不进成员榜单。
        local_done = learn_done_keys(db, viewer_id)

    def is_done(item: ProblemListItem) -> bool | None:
        if done_set is None:
            return None
        if item.platform == Platform.LEARN:
            return item.key in local_done
        return (item.platform, item.key) in done_set

    return [to_entry(item, caches.get((item.platform, item.key)), is_done(item)) for item in items]


def _counts_for_list(
    db: Session,
    list_id: int,
    done_set: set[tuple[str, str]] | None,
    viewer_id: int | None = None,
) -> tuple[int, int]:
    """题单的 (题数, 已完成数)。本地练习用本人客户端 AC 记录。"""
    items = db.exec(select(ProblemListItem).where(ProblemListItem.list_id == list_id)).all()
    if done_set is None:
        return len(items), 0
    local_done: set[str] = set()
    if viewer_id is not None and any(i.platform == Platform.LEARN for i in items):
        from .learning_helpers import learn_done_keys

        local_done = learn_done_keys(db, viewer_id)
    done = sum(
        1
        for item in items
        if (
            item.key in local_done
            if item.platform == Platform.LEARN
            else (item.platform, item.key) in done_set
        )
    )
    return len(items), done


def _children(db: Session, collection_id: int) -> list[ProblemList]:
    return list(
        db.exec(
            select(ProblemList)
            .where(ProblemList.collection_id == collection_id)
            .order_by(ProblemList.position, ProblemList.id)  # type: ignore[attr-defined]
        ).all()
    )


def summarize(
    db: Session,
    problem_list: ProblemList,
    *,
    done_set: set[tuple[str, str]] | None,
    viewer_id: int | None = None,
) -> ProblemListPublic:
    """一个题单或合集的概要，含完成度。

    合集要把下面每个题单的题数加起来——完成度是「这个合集里总共过了多少题」，
    而不是「有几个子题单做完了」。
    """
    info = ProblemListPublic(
        id=problem_list.id or 0,
        kind=problem_list.kind,
        title=problem_list.title,
        description=problem_list.description,
        collection_id=problem_list.collection_id,
        position=problem_list.position,
        created_at=problem_list.created_at,
        updated_at=problem_list.updated_at,
    )

    if problem_list.is_collection:
        children = _children(db, problem_list.id or 0)
        info.item_count = len(children)
        for child in children:
            total, done = _counts_for_list(db, child.id or 0, done_set, viewer_id)
            info.total_problems += total
            info.done_count += done
    else:
        total, done = _counts_for_list(db, problem_list.id or 0, done_set, viewer_id)
        info.item_count = total
        info.total_problems = total
        info.done_count = done

    return info


def detail(
    db: Session,
    problem_list: ProblemList,
    *,
    viewer_id: int | None,
) -> ProblemListDetail:
    """题单详情。题单带题目，合集带子题单。"""
    done_set = solved_keys(db, viewer_id) if viewer_id is not None else None
    base = summarize(db, problem_list, done_set=done_set, viewer_id=viewer_id)

    out = ProblemListDetail(**base.model_dump())
    if problem_list.is_collection:
        out.children = [
            summarize(db, child, done_set=done_set, viewer_id=viewer_id)
            for child in _children(db, problem_list.id or 0)
        ]
    else:
        out.problems = list_problems(db, problem_list, viewer_id=viewer_id)
    return out


def completion_rank(db: Session, problem_list: ProblemList) -> list[ProblemListRankEntry]:
    """题单的完成度排行榜：每个成员过了这个题单里的多少题。

    只统计**有绑定平台账号**的成员。没绑账号的人完成度恒为 0，把他们列进
    来会淹没真正在练的人，也会让人以为「大家都还没开始」。

    合集要先把下面所有题单的题合并起来算——完成度按「这个合集里总共过了
    多少题」，而不是「几个子题单做完了」。
    """
    from .models import Role, User, UserStatus
    from .schemas import ProblemListRankEntry

    # 合集：把子题单的题全收集起来。
    if problem_list.is_collection:
        children = _children(db, problem_list.id or 0)
        child_ids = [c.id or 0 for c in children]
        items = (
            db.exec(
                select(ProblemListItem).where(
                    ProblemListItem.list_id.in_(child_ids)  # type: ignore[attr-defined]
                )
            ).all()
            if child_ids
            else []
        )
    else:
        items = db.exec(
            select(ProblemListItem).where(ProblemListItem.list_id == problem_list.id)
        ).all()

    # 本地练习题的 AC 是客户端自报的，不能算进成员完成度榜单；
    # 留在列表里展示，但不参与 wanted/total 的统计口径。
    rankable = [i for i in items if i.platform != Platform.LEARN]
    total = len(rankable)
    if total == 0:
        return []

    wanted = {(item.platform, item.key) for item in rankable}

    # 有绑定账号的成员。root 不算——它不是成员。
    bound_ids = select(Account.user_id).where(Account.verified_at.is_not(None))  # type: ignore[union-attr]
    members = list(
        db.exec(
            select(User).where(
                User.id.in_(bound_ids),  # type: ignore[attr-defined]
                User.role != Role.ROOT,
                User.status == UserStatus.ACTIVE,
            )
        ).all()
    )

    out: list[ProblemListRankEntry] = []
    for member in members:
        solved = solved_keys(db, member.id or 0)
        hit = solved & wanted
        # 一题没过的也列出来，但排在最后（下面排序保证）。
        done = len(hit)

        # 最后一次过这个题单里的题是哪天。用 daily_progress 反查，
        # 比从时间戳算省事，而且和榜单的口径一致。
        last_date: str | None = None
        if hit:
            rows = db.exec(
                select(DailyProgress)
                .join(Account, Account.id == DailyProgress.account_id)  # type: ignore[arg-type]
                .where(
                    Account.user_id == member.id,
                    DailyProgress.solved_count > 0,
                )
                .order_by(DailyProgress.date.desc())  # type: ignore[attr-defined]
            ).all()
            if rows:
                last_date = rows[0].date

        out.append(
            ProblemListRankEntry(
                user_id=member.id or 0,
                display_name=member.display_name,
                student_id=member.student_id,
                enroll_year=member.enroll_year,
                college_name=member.college_name,
                major_name=member.major_name,
                class_label=member.class_label,
                done_count=done,
                total=total,
                percent=round(done / total * 100) if total else 0,
                last_done_date=last_date,
            )
        )

    # 完成数多的在前；一样多时按完成度（题单总题数相同，所以等价），
    # 再一样就按学号，保证顺序稳定。
    out.sort(key=lambda e: (-e.done_count, e.student_id))
    return out
