"""题单的读写。

读是公开的——题单本身（有哪些题）不算隐私，分享链接给别人看是正常用法。
写只有管理员能动。

进度是另一回事：谁能过哪些题是个人数据，所以只有登录后才带上 ``done``
字段。未登录时完成度显示为 0 而不是「全部未完成」，前端据此隐藏进度条。
"""

from __future__ import annotations

import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, delete, select

from ..db import get_session
from ..deps import get_optional_user, require_admin, require_origin_secret
from ..models import (
    Platform,
    ProblemCache,
    ProblemList,
    ProblemListItem,
    ProblemListKind,
    User,
    utcnow,
)
from ..problemlists import (
    MAX_PROBLEMS_PER_LIST,
    cached_problems,
    completion_rank,
    detail,
    summarize,
)
from ..problems import fetch_all_codeforces, fetch_one, normalize_key, problem_url
from ..schemas import (
    ProblemEntry,
    ProblemItemCreate,
    ProblemItemUpdate,
    ProblemListCreate,
    ProblemListDetail,
    ProblemListPublic,
    ProblemListRank,
    ProblemListUpdate,
    ProblemLookup,
    ReorderRequest,
)

router = APIRouter(
    prefix="/problem-lists",
    tags=["problem-lists"],
    dependencies=[Depends(require_origin_secret)],
)


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[ProblemListPublic])
def list_lists(
    collection_id: int | None = Query(default=None, description="只看某个合集下的题单"),
    kind: str | None = Query(default=None, description="collection 或 list"),
    include_nested: bool = Query(
        default=False,
        description="是否连合集内部的题单一起返回。管理页需要，展示页不需要。",
    ),
    viewer: User | None = Depends(get_optional_user),
    db: Session = Depends(get_session),
) -> list[ProblemListPublic]:
    """题单列表。

    默认返回顶层：所有合集，加上不属于任何合集的题单。这样「和合集平级
    显示的题单」和「收在合集里的题单」都能照顾到。

    ``include_nested=true`` 时连合集内部的题单也一并返回。管理页要这个——
    否则管理员看不到已经放进合集的题单，也就没法给它们加题。
    """
    statement = select(ProblemList)
    if collection_id is not None:
        statement = statement.where(ProblemList.collection_id == collection_id)
    elif kind is None and not include_nested:
        # 顶层：不属于任何合集的，加上所有合集。
        statement = statement.where(ProblemList.collection_id.is_(None))  # type: ignore[union-attr]
    if kind is not None:
        statement = statement.where(ProblemList.kind == kind)

    statement = statement.order_by(ProblemList.position, ProblemList.id)  # type: ignore[attr-defined]
    rows = list(db.exec(statement).all())

    # 完成度要跟这个人已过的题比对，一次算好，别在循环里反复查。
    from ..problemlists import solved_keys

    done_set = solved_keys(db, viewer.id) if viewer is not None else None
    return [
        summarize(db, row, done_set=done_set, viewer_id=viewer.id if viewer else None)
        for row in rows
    ]


@router.get("/{list_id}/rank", response_model=ProblemListRank)
def list_completion_rank(
    list_id: int,
    _viewer: User | None = Depends(get_optional_user),
    db: Session = Depends(get_session),
) -> ProblemListRank:
    """题单完成度排行榜。

    公开可看：谁做了多少不是隐私——练习量榜本来就是公开的。未登录也能看，
    只是不能看自己那一行的高亮（前端处理）。
    """
    problem_list = db.get(ProblemList, list_id)
    if problem_list is None:
        raise HTTPException(status_code=404, detail="题单不存在")

    entries = completion_rank(db, problem_list)
    return ProblemListRank(
        list_id=list_id,
        title=problem_list.title,
        total=entries[0].total if entries else 0,
        entries=entries,
    )


@router.get("/{list_id}", response_model=ProblemListDetail)
def get_list(
    list_id: int,
    viewer: User | None = Depends(get_optional_user),
    db: Session = Depends(get_session),
) -> ProblemListDetail:
    problem_list = db.get(ProblemList, list_id)
    if problem_list is None:
        raise HTTPException(status_code=404, detail="题单不存在")
    return detail(db, problem_list, viewer_id=viewer.id if viewer else None)


# --------------------------------------------------------------------------- #
# 写：题单本身
# --------------------------------------------------------------------------- #


def _check_collection(
    db: Session, collection_id: int | None, *, self_id: int | None = None
) -> None:
    """校验合集引用。只允许两层，所以约束都在这里。"""
    if collection_id is None:
        return
    if self_id is not None and collection_id == self_id:
        raise HTTPException(status_code=400, detail="不能把合集放进自己里")

    parent = db.get(ProblemList, collection_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="合集不存在")
    if not parent.is_collection:
        raise HTTPException(status_code=400, detail="只能放进合集，不能放进题单")
    if parent.collection_id is not None:
        # 父级本身已经属于某个合集，说明在往第三层放。
        raise HTTPException(status_code=400, detail="合集不能再套合集")


@router.post("", response_model=ProblemListPublic, status_code=status.HTTP_201_CREATED)
def create_list(
    payload: ProblemListCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> ProblemListPublic:
    if payload.kind == ProblemListKind.COLLECTION and payload.collection_id is not None:
        raise HTTPException(status_code=400, detail="合集不能再套合集")
    if payload.kind == ProblemListKind.LIST:
        _check_collection(db, payload.collection_id)

    # 排在最后。手动拖拽时再调整。
    last = db.exec(
        select(ProblemList)
        .where(ProblemList.collection_id == payload.collection_id)
        .order_by(ProblemList.position.desc())  # type: ignore[attr-defined]
        .limit(1)
    ).first()

    problem_list = ProblemList(
        kind=payload.kind,
        title=payload.title,
        description=payload.description,
        collection_id=payload.collection_id,
        position=(last.position + 1) if last else 0,
        admin_note=payload.admin_note,
        created_by=admin.id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(problem_list)
    db.commit()
    db.refresh(problem_list)
    return summarize(db, problem_list, done_set=None)


@router.patch("/{list_id}", response_model=ProblemListPublic)
def update_list(
    list_id: int,
    payload: ProblemListUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> ProblemListPublic:
    problem_list = db.get(ProblemList, list_id)
    if problem_list is None:
        raise HTTPException(status_code=404, detail="题单不存在")

    if payload.title is not None:
        problem_list.title = payload.title
    if payload.description is not None:
        problem_list.description = payload.description
    if payload.admin_note is not None:
        problem_list.admin_note = payload.admin_note

    if payload.set_collection:
        if problem_list.is_collection and payload.collection_id is not None:
            raise HTTPException(status_code=400, detail="合集不能再套合集")
        _check_collection(db, payload.collection_id, self_id=list_id)
        problem_list.collection_id = payload.collection_id

    problem_list.updated_at = utcnow()
    db.add(problem_list)
    db.commit()
    db.refresh(problem_list)
    return summarize(db, problem_list, done_set=None)


@router.delete("/{list_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_list(
    list_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> None:
    """删题单。删合集时下面的题单不跟着删，只是失去归属。"""
    problem_list = db.get(ProblemList, list_id)
    if problem_list is None:
        raise HTTPException(status_code=404, detail="题单不存在")

    for item in db.exec(select(ProblemListItem).where(ProblemListItem.list_id == list_id)).all():
        db.delete(item)

    # 子题单的 collection_id 置空，而不是级联删除——合集常常只是分组，
    # 删分组不该把内容一起带走。
    for child in db.exec(select(ProblemList).where(ProblemList.collection_id == list_id)).all():
        child.collection_id = None
        db.add(child)

    db.delete(problem_list)
    db.commit()


@router.post("/reorder", response_model=list[ProblemListPublic])
def reorder_lists(
    payload: ReorderRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> list[ProblemListPublic]:
    """拖拽排序题单。按给出的 id 顺序重排。"""
    for index, list_id in enumerate(payload.ids):
        problem_list = db.get(ProblemList, list_id)
        if problem_list is None:
            continue
        problem_list.position = index
        db.add(problem_list)
    db.commit()
    return [
        summarize(db, row, done_set=None)
        for row in db.exec(select(ProblemList).where(ProblemList.id.in_(payload.ids))).all()  # type: ignore[attr-defined]
    ]


# --------------------------------------------------------------------------- #
# 写：题目
# --------------------------------------------------------------------------- #


@router.get("/lookup/problem", response_model=ProblemLookup)
def lookup_problem(
    platform: str = Query(description="平台"),
    key: str = Query(description="题号"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> ProblemLookup:
    """按题号查题目信息。

    管理员在输入框里填完题号、离开焦点时调这个，把题名显示出来。
    这样填错了当场就能看见——光看题号「1004A」看不出是哪个平台哪道题。
    """
    from ..learning_helpers import learning_enabled

    if platform not in Platform.ALL and not (platform == Platform.LEARN and learning_enabled()):
        raise HTTPException(status_code=400, detail="未知的平台")

    # 规范化必须在查缓存之前：用户填的是裸数字（牛客），缓存里存的是
    # 带来源前缀的 key，不转的话永远查不到。
    key = normalize_key(platform, key)
    if not key:
        raise HTTPException(status_code=400, detail="题号不能为空")

    if platform == Platform.LEARN:
        from ..learning_helpers import learn_problem_or_400, upsert_learn_cache

        problem = learn_problem_or_400(db, key)
        cached = upsert_learn_cache(db, problem)
        db.commit()
        return ProblemLookup(
            platform=platform,
            key=key,
            name=cached.name,
            rating=None,
            tags=json.loads(cached.tags_json or "[]"),
            url=cached.url,
            cached=True,
        )

    cached = cached_problems(db, platform, [key]).get(key)
    if cached is not None:
        return ProblemLookup(
            platform=platform,
            key=key,
            name=cached.name,
            rating=cached.rating,
            tags=json.loads(cached.tags_json or "[]"),
            url=cached.url,
            missing_reason=cached.missing_reason,
            cached=True,
        )

    # 缓存里没有，去抓一次并存下。CF 走全量接口，其余逐题。
    info = _fetch_and_cache(db, platform, key)
    return ProblemLookup(
        platform=platform,
        key=key,
        name=info.name,
        rating=info.rating,
        tags=info.tags,
        url=info.url,
        missing_reason=info.missing_reason,
        cached=False,
    )


def _fetch_and_cache(db: Session, platform: str, key: str):
    """抓一道题的元信息并存进缓存。

    **抓失败不写缓存。** 早先的做法是失败也存一行，想着「免得每次重试」，
    结果是：一次网络抖动会把「暂时查不到」固化成永久结果，之后再怎么刷新
    都读的是那行坏缓存。缓存只该存确定的答案。

    「这道题确实不存在」不算失败，那种会带上 missing_reason 正常存下来，
    免得每次都为一道不存在的题去请求对方服务器。
    """
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    try:
        with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as client:
            info = fetch_one(client, platform, key)
    except Exception as exc:  # noqa: BLE001 - 抓取失败不该让整个请求 500
        from ..problems import ProblemInfo

        return ProblemInfo(
            platform=platform,
            key=key,
            url=problem_url(platform, key),
            missing_reason=f"抓取失败：{exc}",
        )

    # CF 单题查询会返回「需要刷新题库」——那是提示不是结果，不该缓存。
    if info.missing_reason and "需要刷新" in info.missing_reason:
        return info

    row = db.exec(
        select(ProblemCache).where(ProblemCache.platform == platform, ProblemCache.key == key)
    ).first()
    if row is None:
        row = ProblemCache(platform=platform, key=key)
        db.add(row)

    row.name = info.name
    row.rating = info.rating
    row.tags_json = json.dumps(info.tags, ensure_ascii=False)
    row.url = info.url or problem_url(platform, key)
    row.solved_count = info.solved_count
    row.missing_reason = info.missing_reason
    row.fetched_at = utcnow()
    db.commit()
    return info


@router.post("/purge-problem-cache")
def purge_problem_cache(
    platform: str | None = Query(default=None, description="只清这个平台，不填则全清"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> dict:
    """清掉题目信息缓存，让它下次访问时重新抓。

    **为什么需要这个接口**：缓存没有过期时间（题名和难度基本不变，设过期
    只会白白请求对方服务器），但抓取逻辑本身会改。改完之后缓存里还是按
    旧逻辑抓出来的结果，而且永远不会被覆盖——牛客就踩过：地址拼接修好了，
    缓存里存的还是修复前拼出来的搜索页链接。

    清掉之后不影响题单本身，只是下次打开题单时会重新抓一遍题名。抓取要
    逐题请求（洛谷、牛客、AtCoder 都是），题多的时候会慢一些，所以做成
    显式操作而不是自动触发。
    """
    statement = delete(ProblemCache)
    if platform:
        from ..learning_helpers import learning_enabled

        if platform not in Platform.ALL and not (platform == Platform.LEARN and learning_enabled()):
            raise HTTPException(status_code=400, detail="未知的平台")
        statement = statement.where(ProblemCache.platform == platform)  # type: ignore[arg-type]

    result = db.exec(statement)
    db.commit()
    return {"deleted": result.rowcount or 0, "platform": platform}


@router.post("/refresh-codeforces")
def refresh_codeforces(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> dict:
    """整批刷新 CF 题库。

    一次请求拿回一万多题，所以不做逐题抓取——那要一万多次请求。
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        with httpx.Client(headers=headers, timeout=60, follow_redirects=True) as client:
            all_problems = fetch_all_codeforces(client)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"抓取失败：{exc}") from exc

    existing = {
        row.key: row
        for row in db.exec(
            select(ProblemCache).where(ProblemCache.platform == Platform.CODEFORCES)
        ).all()
    }

    added = 0
    updated = 0
    for info in all_problems:
        row = existing.get(info.key)
        if row is None:
            db.add(
                ProblemCache(
                    platform=Platform.CODEFORCES,
                    key=info.key,
                    name=info.name,
                    rating=info.rating,
                    tags_json=json.dumps(info.tags, ensure_ascii=False),
                    url=info.url,
                    solved_count=info.solved_count,
                    fetched_at=utcnow(),
                )
            )
            added += 1
            continue

        # 已有的也要更新：之前可能有抓失败留下的空壳，跳过的话它永远
        # 补不上。题目名偶尔也会被官方改。
        #
        # solved_count 也算进「变了没」的判断：这个字段是后加的，老数据
        # 全是空的，不比对的话重跑一次全量抓取也不会把值补上。
        if (
            row.name != info.name
            or row.rating != info.rating
            or row.solved_count != info.solved_count
        ):
            updated += 1
        row.name = info.name
        row.rating = info.rating
        row.tags_json = json.dumps(info.tags, ensure_ascii=False)
        row.url = info.url
        row.solved_count = info.solved_count
        row.missing_reason = ""
        row.fetched_at = utcnow()

    db.commit()

    return {"total": len(all_problems), "added": added, "updated": updated}


@router.post(
    "/{list_id}/problems", response_model=ProblemEntry, status_code=status.HTTP_201_CREATED
)
def add_problem(
    list_id: int,
    payload: ProblemItemCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> ProblemEntry:
    problem_list = db.get(ProblemList, list_id)
    if problem_list is None:
        raise HTTPException(status_code=404, detail="题单不存在")
    if problem_list.is_collection:
        raise HTTPException(status_code=400, detail="合集里只能放题单，不能放题目")

    count = len(db.exec(select(ProblemListItem).where(ProblemListItem.list_id == list_id)).all())
    if count >= MAX_PROBLEMS_PER_LIST:
        raise HTTPException(status_code=400, detail=f"一个题单最多 {MAX_PROBLEMS_PER_LIST} 题")

    # 存进题单之前规范化。存成裸数字的话，过题记录（带前缀）比对不上，
    # 完成度永远算成 0。
    key = normalize_key(payload.platform, payload.key)
    if not key:
        raise HTTPException(status_code=400, detail="题号不能为空")

    duplicate = db.exec(
        select(ProblemListItem).where(
            ProblemListItem.list_id == list_id,
            ProblemListItem.platform == payload.platform,
            ProblemListItem.key == key,
        )
    ).first()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="这道题已经在题单里了")

    last = db.exec(
        select(ProblemListItem)
        .where(ProblemListItem.list_id == list_id)
        .order_by(ProblemListItem.position.desc())  # type: ignore[attr-defined]
        .limit(1)
    ).first()

    item = ProblemListItem(
        list_id=list_id,
        platform=payload.platform,
        key=key,
        position=(last.position + 1) if last else 0,
        note=payload.note,
        created_at=utcnow(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    # 本地练习题从 learning 表取题名写进缓存，不走外部抓取。
    if payload.platform == Platform.LEARN:
        from ..learning_helpers import learn_problem_or_400, upsert_learn_cache

        cache = upsert_learn_cache(db, learn_problem_or_400(db, key))
        db.commit()
        from ..problemlists import to_entry

        return to_entry(item, cache, None)

    # 加完顺手把题目信息抓下来，列表里立刻能看到题名。
    cache = cached_problems(db, payload.platform, [key]).get(key)
    if cache is None:
        info = _fetch_and_cache(db, payload.platform, key)
        cache = ProblemCache(
            platform=payload.platform,
            key=key,
            name=info.name,
            rating=info.rating,
            tags_json=json.dumps(info.tags, ensure_ascii=False),
            url=info.url,
            missing_reason=info.missing_reason,
        )

    from ..problemlists import to_entry

    return to_entry(item, cache, None)


@router.patch("/{list_id}/problems/{item_id}", response_model=ProblemEntry)
def update_problem(
    list_id: int,
    item_id: int,
    payload: ProblemItemUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> ProblemEntry:
    item = db.get(ProblemListItem, item_id)
    if item is None or item.list_id != list_id:
        raise HTTPException(status_code=404, detail="题目不在这个题单里")

    if payload.note is not None:
        item.note = payload.note
    db.add(item)
    db.commit()
    db.refresh(item)

    from ..problemlists import to_entry

    cache = cached_problems(db, item.platform, [item.key]).get(item.key)
    return to_entry(item, cache, None)


@router.delete("/{list_id}/problems/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_problem(
    list_id: int,
    item_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> None:
    item = db.get(ProblemListItem, item_id)
    if item is None or item.list_id != list_id:
        raise HTTPException(status_code=404, detail="题目不在这个题单里")
    db.delete(item)
    db.commit()


@router.post("/{list_id}/problems/reorder", response_model=list[ProblemEntry])
def reorder_problems(
    list_id: int,
    payload: ReorderRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> list[ProblemEntry]:
    """拖拽排序题单里的题目。"""
    problem_list = db.get(ProblemList, list_id)
    if problem_list is None:
        raise HTTPException(status_code=404, detail="题单不存在")

    for index, item_id in enumerate(payload.ids):
        item = db.get(ProblemListItem, item_id)
        if item is None or item.list_id != list_id:
            continue
        item.position = index
        db.add(item)
    db.commit()

    from ..problemlists import list_problems

    return list_problems(db, problem_list, viewer_id=None)
