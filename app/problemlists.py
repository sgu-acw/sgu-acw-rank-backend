"""题单的读取与完成度计算。

完成度 = 完成的题数 / 总题数。判断「完成」的办法是把题单里的题号与这个人
已过的题号做集合比对——两边都用平台原生写法，所以不需要任何格式转换。

一个人可能绑了多个账号（主号加小号），任一账号过了就算过了。这与练习量榜
的口径一致：那里也是把同一平台的主号小号合并统计的。

未登录时不显示进度：谁能过哪些题是个人数据，不该公开给所有人看。
"""

from __future__ import annotations

import json

from sqlalchemy import func
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
    accounts = db.exec(
        select(Account).where(
            Account.user_id == user_id,
            Account.verified_at.is_not(None),  # type: ignore[union-attr]
        )
    ).all()
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


def _count_items(
    items: list[ProblemListItem],
    done_set: set[tuple[str, str]] | None,
    local_done: set[str],
) -> tuple[int, int]:
    if done_set is None:
        return len(items), 0
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


def summarize_many(
    db: Session,
    problem_lists: list[ProblemList],
    *,
    done_set: set[tuple[str, str]] | None,
    viewer_id: int | None = None,
) -> list[ProblemListPublic]:
    """批量生成题单/合集概要，避免对每个题单和子题单逐条查库。"""
    if not problem_lists:
        return []

    collection_ids = [pl.id or 0 for pl in problem_lists if pl.is_collection]
    children_by_col: dict[int, list[ProblemList]] = {}
    if collection_ids:
        all_children = db.exec(
            select(ProblemList)
            .where(ProblemList.collection_id.in_(collection_ids))  # type: ignore[attr-defined]
            .order_by(ProblemList.position, ProblemList.id)  # type: ignore[attr-defined]
        ).all()
        for child in all_children:
            if child.collection_id is not None:
                children_by_col.setdefault(child.collection_id, []).append(child)

    leaf_ids: set[int] = set()
    for pl in problem_lists:
        if pl.is_collection:
            for child in children_by_col.get(pl.id or 0, []):
                if child.id:
                    leaf_ids.add(child.id)
        elif pl.id:
            leaf_ids.add(pl.id)

    items_by_list: dict[int, list[ProblemListItem]] = {}
    has_learn = False
    if leaf_ids:
        for item in db.exec(
            select(ProblemListItem).where(
                ProblemListItem.list_id.in_(leaf_ids)  # type: ignore[attr-defined]
            )
        ).all():
            items_by_list.setdefault(item.list_id, []).append(item)
            if item.platform == Platform.LEARN:
                has_learn = True

    local_done: set[str] = set()
    if done_set is not None and viewer_id is not None and has_learn:
        from .learning_helpers import learn_done_keys

        local_done = learn_done_keys(db, viewer_id)

    result: list[ProblemListPublic] = []
    for pl in problem_lists:
        info = ProblemListPublic(
            id=pl.id or 0,
            kind=pl.kind,
            title=pl.title,
            description=pl.description,
            collection_id=pl.collection_id,
            position=pl.position,
            created_at=pl.created_at,
            updated_at=pl.updated_at,
        )
        if pl.is_collection:
            children = children_by_col.get(pl.id or 0, [])
            info.item_count = len(children)
            for child in children:
                total, done = _count_items(
                    items_by_list.get(child.id or 0, []), done_set, local_done
                )
                info.total_problems += total
                info.done_count += done
        else:
            total, done = _count_items(items_by_list.get(pl.id or 0, []), done_set, local_done)
            info.item_count = total
            info.total_problems = total
            info.done_count = done
        result.append(info)
    return result


def _rankable_wanted(db: Session, problem_list: ProblemList) -> set[tuple[str, str]]:
    """收集题单或合集下可计入完成度榜单的外部平台题目（去重）。"""
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
    # 本地练习题的 AC 是客户端自报的，不参与成员完成度榜单口径。
    return {(i.platform, i.key) for i in items if i.platform != Platform.LEARN}


def rankable_problem_count(db: Session, problem_list: ProblemList) -> int:
    """题单或合集中参与完成度排行榜的去重题目数。"""
    return len(_rankable_wanted(db, problem_list))


def completion_rank(db: Session, problem_list: ProblemList) -> list[ProblemListRankEntry]:
    """题单的完成度排行榜：每个成员过了这个题单里的多少题。

    只统计**有绑定平台账号**的成员。批量读取快照与最近练习日，避免 N+1 查询。
    """
    from .models import Role, User, UserStatus
    from .schemas import ProblemListRankEntry

    wanted = _rankable_wanted(db, problem_list)
    total = len(wanted)
    if total == 0:
        return []

    # 有绑定账号的校内成员。root 和校外账号不计入。
    bound_ids = select(Account.user_id).where(Account.verified_at.is_not(None))  # type: ignore[union-attr]
    members = list(
        db.exec(
            select(User).where(
                User.id.in_(bound_ids),  # type: ignore[attr-defined]
                User.role.in_((Role.ADMIN, Role.MEMBER)),  # type: ignore[attr-defined]
                User.status == UserStatus.ACTIVE,
            )
        ).all()
    )
    if not members:
        return []

    member_ids = [m.id or 0 for m in members]
    wanted_platforms = list({platform for platform, _ in wanted})

    # 批量读取相关平台的已验证账号快照，一次查询按人汇总。
    solved_by_user: dict[int, set[tuple[str, str]]] = {}
    snapshots = db.exec(
        select(Account.user_id, Account.platform, SolvedSnapshot.problems_json)
        .join(SolvedSnapshot, SolvedSnapshot.account_id == Account.id)  # type: ignore[arg-type]
        .where(
            Account.user_id.in_(member_ids),  # type: ignore[attr-defined]
            Account.verified_at.is_not(None),  # type: ignore[union-attr]
            Account.platform.in_(wanted_platforms),  # type: ignore[attr-defined]
        )
    ).all()
    for uid, platform, payload in snapshots:
        try:
            raw_keys = json.loads(payload)
        except (TypeError, ValueError):
            continue
        bucket = solved_by_user.setdefault(uid, set())
        for k in raw_keys:
            pair = (platform, str(k))
            if pair in wanted:
                bucket.add(pair)

    # 批量读取每位成员最近一次有刷题记录的日期（SQL MAX 聚合）。
    last_dates: dict[int, str] = {}
    for uid, latest in db.exec(
        select(Account.user_id, func.max(DailyProgress.date))
        .join(DailyProgress, DailyProgress.account_id == Account.id)  # type: ignore[arg-type]
        .where(
            Account.user_id.in_(member_ids),  # type: ignore[attr-defined]
            Account.verified_at.is_not(None),  # type: ignore[union-attr]
            DailyProgress.solved_count > 0,
        )
        .group_by(Account.user_id)
    ).all():
        if latest:
            last_dates[uid] = latest

    out: list[ProblemListRankEntry] = []
    for member in members:
        hit = solved_by_user.get(member.id or 0, set())
        done = len(hit)
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
                last_done_date=last_dates.get(member.id or 0) if done else None,
            )
        )

    out.sort(key=lambda e: (-e.done_count, e.student_id))
    return out
