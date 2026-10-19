"""文章（博客/公告）的核心逻辑。

文章和公告是同一张表：公告只是「会弹窗」的文章。弹窗互斥由这里保证——
全站最多一篇 ``is_popup=True``，开新的自动关旧的。

slug 从标题生成，撞了加序号。中文直接留着——浏览器会自动百分号编码，
``/blog/100-入门指南`` 这种地址是可用的（老文章就是这格式）。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

from sqlalchemy import update
from sqlmodel import Session, select

from .models import Post, utcnow


class PostError(Exception):
    """文章业务的错误，带着面向用户的中文消息。"""


def slugify(title: str) -> str:
    """标题转 slug。

    空白和斜杠换成横线，去掉路径不安全的字符。中文不动——老文章就是
    中文 slug，新文章保持一致。
    """
    s = unicodedata.normalize("NFKC", title).strip()
    s = re.sub(r"[\s/\\]+", "-", s)
    s = re.sub(r"[^\w\-]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "post"


def unique_slug(session: Session, title: str, *, exclude_id: int | None = None) -> str:
    """给标题算一个不撞的 slug。"""
    base = slugify(title)
    slug = base
    n = 2
    while True:
        existing = session.exec(select(Post).where(Post.slug == slug)).first()
        if existing is None or existing.id == exclude_id:
            return slug
        slug = f"{base}-{n}"
        n += 1


def lock_post(session: Session, post_id: int) -> Post | None:
    session.exec(update(Post).where(Post.id == post_id).values(id=Post.id))
    return session.get(Post, post_id, populate_existing=True)


def set_popup(
    session: Session,
    post: Post,
    *,
    on: bool,
    until: datetime | None = None,
) -> None:
    """开关弹窗。开的时候把别的弹窗文章关掉——全站只弹一篇。"""
    post = lock_post(session, post.id)
    if post is None:
        raise PostError("没有这篇文章")
    if on:
        others = session.exec(
            select(Post).where(Post.is_popup.is_(True), Post.id != post.id)  # type: ignore[union-attr]
        ).all()
        for other in others:
            other.is_popup = False
            other.popup_until = None
            session.add(other)
    post.is_popup = on
    post.popup_until = until if on else None
    session.add(post)
    session.commit()


def active_popup(session: Session) -> Post | None:
    """当前该弹的公告。没有就返回 None。

    条件：开了弹窗开关、已上架、没过期。多篇同时满足时取最新的——
    ``set_popup`` 保证了最多一篇开着，这里的排序是双保险。
    """
    now = datetime.now(UTC)
    rows = session.exec(
        select(Post).where(Post.is_popup.is_(True), Post.published.is_(True))  # type: ignore[union-attr]
    ).all()
    live = [r for r in rows if r.popup_until is None or r.popup_until > now]
    if not live:
        return None
    return max(live, key=lambda r: r.updated_at)


def touch(post: Post) -> None:
    """更新 updated_at。

    这是弹窗的版本指纹：内容一改时间戳就变，浏览器里的「已确认」记录对不上
    新版本，就会再弹一次。
    """
    post.updated_at = utcnow()


def sweep_expired_popups(session: Session, *, now: datetime | None = None) -> int:
    """撤销到点的公告弹窗。返回清了几条。

    ``is_popup`` 是开关，``popup_until`` 是自动下架时刻——过点了就该把开关
    也关掉，不然管理页一直显示「取消公告」，看着像没有撤销。由定时任务
    调，别放在读接口里偷偷写。
    """
    moment = now or datetime.now(UTC)
    result = session.exec(
        update(Post)
        .where(
            Post.is_popup.is_(True),
            Post.popup_until.is_not(None),
            Post.popup_until <= moment,
        )
        .values(is_popup=False, popup_until=None)
    )
    session.commit()
    return result.rowcount
