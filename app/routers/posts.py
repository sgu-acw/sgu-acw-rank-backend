"""文章（博客/公告）的接口。

公开：列表、详情、当前弹窗、RSS。
作者区：新建、改自己写的（管理员能改所有的）、删除（管理员）。
管理员独有：弹窗开关、上下架、看未发布的。
"""

from __future__ import annotations

import re
from datetime import datetime
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from .. import posts as posts_mod
from ..deps import get_current_user, get_session, require_admin
from ..models import Post, User

router = APIRouter(tags=["posts"])


# --------------------------------------------------------------------------- #
# 出参模型
# --------------------------------------------------------------------------- #


class PostSummary(BaseModel):
    id: int
    slug: str
    title: str
    excerpt: str
    sort_order: int
    is_popup: bool
    published_at: str  # created_at 的 ISO，列表显示用
    author_name: str


class PostDetail(PostSummary):
    body: str


class PopupInfo(BaseModel):
    """当前该弹的公告。version 是内容指纹，改了内容版本就变。"""

    id: int
    slug: str
    title: str
    body: str
    version: int  # updated_at 的 epoch 秒


class AdminPostSummary(PostSummary):
    published: bool
    author_id: int
    popup_until: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminPostDetail(AdminPostSummary):
    body: str


# --------------------------------------------------------------------------- #
# 拼装
# --------------------------------------------------------------------------- #


def _excerpt(body: str, limit: int = 200) -> str:
    """从 markdown 原文抽一段纯文本做摘要。

    和 blog/index.astro 里的老逻辑一致：去 frontmatter、去标记、截断。
    """
    text = re.sub(r"^---[\s\S]*?---\n*", "", body)
    text = re.sub(r"#+\s", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_~`]", "", text)
    text = re.sub(r"\|.*\|", "", text)
    text = re.sub(r"^---+$", "", text, flags=re.M)
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    return text.strip()[:limit]


def _to_summary(post: Post, author: User | None) -> PostSummary:
    return PostSummary(
        id=post.id or 0,
        slug=post.slug,
        title=post.title,
        excerpt=_excerpt(post.body),
        sort_order=post.sort_order,
        is_popup=post.is_popup,
        published_at=post.created_at.isoformat(),
        author_name=author.display_name if author else "",
    )


def _to_detail(post: Post, author: User | None) -> PostDetail:
    return PostDetail(**_to_summary(post, author).model_dump(), body=post.body)


def _to_admin(post: Post, author: User | None) -> AdminPostSummary:
    return AdminPostSummary(
        **_to_summary(post, author).model_dump(),
        published=post.published,
        author_id=post.author_id,
        popup_until=post.popup_until,
        created_at=post.created_at,
        updated_at=post.updated_at,
    )


def _author_map(session: Session, posts: list[Post]) -> dict[int, User]:
    ids = {p.author_id for p in posts}
    if not ids:
        return {}
    return {u.id: u for u in session.exec(select(User).where(User.id.in_(ids))).all()}


# --------------------------------------------------------------------------- #
# 公开接口
# --------------------------------------------------------------------------- #


@router.get("/posts", response_model=list[PostSummary])
def list_posts(session: Session = Depends(get_session)) -> list[PostSummary]:
    """已发布的文章，按发布时间倒序；sort_order 只用来手动置顶。"""
    rows = session.exec(
        select(Post)
        .where(Post.published.is_(True))
        .order_by(Post.sort_order.desc(), Post.created_at.desc())  # type: ignore[union-attr]
    ).all()
    authors = _author_map(session, rows)
    return [_to_summary(p, authors.get(p.author_id)) for p in rows]


@router.get("/posts/popup", response_model=PopupInfo | None)
def get_popup(session: Session = Depends(get_session)) -> PopupInfo | None:
    """当前该弹的公告。

    返回 None 表示没有。前端拿 version 跟 localStorage 里记的对一遍，
    对不上就弹——改内容会推高版本号，于是确认过的人也会再看到。
    """
    post = posts_mod.active_popup(session)
    if post is None:
        return None
    return PopupInfo(
        id=post.id or 0,
        slug=post.slug,
        title=post.title,
        body=post.body,
        # 毫秒级：秒级粒度下「改完立刻再拉」可能拿到同一个版本号。
        version=int(post.updated_at.timestamp() * 1000),
    )


@router.get("/posts/rss.xml")
def rss(session: Session = Depends(get_session)) -> Response:
    """RSS 2.0 订阅。"""
    rows = session.exec(
        select(Post).where(Post.published.is_(True)).order_by(Post.created_at.desc())  # type: ignore[union-attr]
    ).all()

    items = []
    for p in rows:
        items.append(
            "<item>"
            f"<title>{escape(p.title)}</title>"
            f"<link>https://sgu-acw.zh314.xyz/blog/{escape(p.slug)}</link>"
            f"<guid isPermaLink=\"false\">post-{p.id}-{int(p.updated_at.timestamp())}</guid>"
            f"<pubDate>{p.created_at.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
            f"<description>{escape(_excerpt(p.body))}</description>"
            "</item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>算法竞赛工作坊</title>"
        "<link>https://sgu-acw.zh314.xyz/blog</link>"
        "<description>工作坊的博客与公告</description>"
        + "".join(items)
        + "</channel></rss>"
    )
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


@router.get("/posts/{slug}", response_model=PostDetail)
def get_post(slug: str, session: Session = Depends(get_session)) -> PostDetail:
    post = session.exec(select(Post).where(Post.slug == slug)).first()
    if post is None or not post.published:
        raise HTTPException(status_code=404, detail="没有这篇文章")
    author = session.get(User, post.author_id)
    return _to_detail(post, author)


# --------------------------------------------------------------------------- #
# 写作区：作者或管理员
# --------------------------------------------------------------------------- #


def _require_author(user: User = Depends(get_current_user)) -> User:
    """能写文章的人：管理员，或者被勾了「可发文章」的成员。"""
    if user.is_admin or user.can_post:
        return user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="没有发文权限")


class PostCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = ""
    sort_order: int = 0
    slug: str | None = Field(default=None, max_length=200)


class PostUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = None
    sort_order: int | None = None


class PopupToggle(BaseModel):
    on: bool
    until: datetime | None = None


class PublishToggle(BaseModel):
    published: bool


@router.get("/admin/posts", response_model=list[AdminPostSummary])
def admin_list(
    session: Session = Depends(get_session),
    _author: User = Depends(_require_author),
) -> list[AdminPostSummary]:
    """所有文章含未发布的。作者和管理员共用这一张列表。"""
    rows = session.exec(select(Post).order_by(Post.created_at.desc())).all()
    authors = _author_map(session, rows)
    return [_to_admin(p, authors.get(p.author_id)) for p in rows]


@router.get("/admin/posts/{post_id}", response_model=AdminPostDetail)
def admin_get(
    post_id: int,
    session: Session = Depends(get_session),
    _author: User = Depends(_require_author),
) -> AdminPostDetail:
    post = session.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="没有这篇文章")
    author = session.get(User, post.author_id)
    return AdminPostDetail(**_to_admin(post, author).model_dump(), body=post.body)


@router.post("/admin/posts", status_code=status.HTTP_201_CREATED, response_model=AdminPostDetail)
def admin_create(
    payload: PostCreate,
    session: Session = Depends(get_session),
    user: User = Depends(_require_author),
) -> AdminPostDetail:
    slug = payload.slug.strip() if payload.slug else ""
    slug = slug or posts_mod.unique_slug(session, payload.title)
    # 手动给的 slug 也要唯一
    if session.exec(select(Post).where(Post.slug == slug)).first():
        raise HTTPException(status_code=400, detail="这个 slug 已经被用了")

    post = Post(
        slug=slug,
        title=payload.title,
        body=payload.body,
        sort_order=payload.sort_order,
        author_id=user.id or 0,
    )
    session.add(post)
    session.commit()
    session.refresh(post)
    return AdminPostDetail(**_to_admin(post, user).model_dump(), body=post.body)


@router.put("/admin/posts/{post_id}", response_model=AdminPostDetail)
def admin_update(
    post_id: int,
    payload: PostUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(_require_author),
) -> AdminPostDetail:
    """改标题/正文/排序。作者只能改自己的，管理员改谁的都行。"""
    post = posts_mod.lock_post(session, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="没有这篇文章")
    if post.author_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="只能改自己写的文章")

    content_changed = (
        payload.title is not None and payload.title != post.title
    ) or (payload.body is not None and payload.body != post.body)
    if payload.title is not None:
        post.title = payload.title
    if payload.body is not None:
        post.body = payload.body
    if payload.sort_order is not None:
        post.sort_order = payload.sort_order
    if content_changed:
        posts_mod.touch(post)
    session.add(post)
    session.commit()
    session.refresh(post)

    author = session.get(User, post.author_id)
    return AdminPostDetail(**_to_admin(post, author).model_dump(), body=post.body)


@router.delete("/admin/posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete(
    post_id: int,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> Response:
    """删文章。只有管理员能删——成员写的也只能管理员撤。"""
    post = session.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="没有这篇文章")
    session.delete(post)
    session.commit()
    return Response(status_code=204)


@router.post("/admin/posts/{post_id}/popup", response_model=AdminPostSummary)
def admin_toggle_popup(
    post_id: int,
    payload: PopupToggle,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> AdminPostSummary:
    """开关弹窗。只有管理员——公告面向全站，不能让成员随手弹。"""
    post = session.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="没有这篇文章")
    posts_mod.set_popup(session, post, on=payload.on, until=payload.until)
    session.refresh(post)
    author = session.get(User, post.author_id)
    return _to_admin(post, author)


@router.post("/admin/posts/{post_id}/publish", response_model=AdminPostSummary)
def admin_toggle_publish(
    post_id: int,
    payload: PublishToggle,
    session: Session = Depends(get_session),
    user: User = Depends(_require_author),
) -> AdminPostSummary:
    """上下架。作者能下自己的，管理员上下都行。"""
    post = posts_mod.lock_post(session, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="没有这篇文章")
    if post.author_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="只能下架自己写的文章")
    changed = post.published != payload.published
    post.published = payload.published
    if not payload.published:
        # 下架连着弹窗一起关
        post.is_popup = False
        post.popup_until = None
    if changed:
        posts_mod.touch(post)
    session.add(post)
    session.commit()
    session.refresh(post)
    author = session.get(User, post.author_id)
    return _to_admin(post, author)
