"""文章（博客/公告）的测试。

覆盖：slug 生成、弹窗互斥、公开接口、写作权限、RSS。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import posts

ROOT_STUDENT_ID = "00000000000"
ROOT_PASSWORD = "devpassword123"
MEMBER_STUDENT_ID = "24125023001"
MEMBER_PASSWORD = "xzxze123456"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SGU_ACW_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SGU_ACW_COOKIE_SECURE", "false")
    monkeypatch.setenv("SGU_ACW_ORIGIN_SECRET", "")
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_STUDENT_ID", ROOT_STUDENT_ID)
    monkeypatch.setenv("SGU_ACW_BOOTSTRAP_ROOT_PASSWORD", ROOT_PASSWORD)

    from app import db as db_module
    from app.config import get_settings

    get_settings.cache_clear()
    db_module.reset_engine()
    db_module.init_db()
    yield
    db_module.reset_engine()
    get_settings.cache_clear()


@pytest.fixture()
def db(env):
    from app.db import get_engine

    with Session(get_engine()) as session:
        yield session


@pytest.fixture()
def client(env):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def root_client(env):
    from app.main import create_app

    with TestClient(create_app()) as c:
        r = c.post(
            "/api/auth/login",
            json={"student_id": ROOT_STUDENT_ID, "password": ROOT_PASSWORD},
        )
        assert r.status_code == 200, r.text
        yield c


@pytest.fixture()
def member_client(env, root_client):
    r = root_client.post(
        "/api/admin/users",
        json={
            "student_id": MEMBER_STUDENT_ID,
            "password": MEMBER_PASSWORD,
            "display_name": "队员甲",
        },
    )
    assert r.status_code in (200, 201), r.text

    from app.main import create_app

    with TestClient(create_app()) as c:
        r = c.post(
            "/api/auth/login",
            json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD},
        )
        assert r.status_code == 200, r.text
        yield c


def make_post(
    session: Session,
    *,
    title: str = "测试文章",
    body: str = "正文",
    slug: str | None = None,
    sort_order: int = 0,
    published: bool = True,
    author_id: int | None = None,
):
    from app.models import Post, User

    # posts.author_id 有外键，作者必须真实存在。库里没人的话现场建一个。
    if author_id is None:
        author = session.exec(select(User)).first()
        if author is None:
            author = User(
                student_id="24125023099",
                display_name="作者",
                password_hash="x",
                enroll_year=2024,
                level=1,
                college_code="25",
                major_code="02",
                class_no=3,
            )
            session.add(author)
            session.commit()
        author_id = author.id or 0

    post = Post(
        slug=slug or posts.unique_slug(session, title),
        title=title,
        body=body,
        sort_order=sort_order,
        published=published,
        author_id=author_id,
    )
    session.add(post)
    session.commit()
    session.refresh(post)
    return post


# --------------------------------------------------------------------------- #
# slug
# --------------------------------------------------------------------------- #


def test_slugify_chinese_kept():
    """中文 slug 原样保留——老文章就是中文地址。"""
    assert posts.slugify("入门指南") == "入门指南"
    assert posts.slugify("Hello World") == "Hello-World"
    assert posts.slugify("a/b\\c d") == "a-b-c-d"


def test_unique_slug_dedup(db):
    """撞了加序号。"""
    make_post(db, title="同名", slug="同名")
    assert posts.unique_slug(db, "同名") == "同名-2"
    make_post(db, title="同名2", slug="同名-2")
    assert posts.unique_slug(db, "同名") == "同名-3"


# --------------------------------------------------------------------------- #
# 弹窗
# --------------------------------------------------------------------------- #


def test_popup_is_exclusive(db):
    """开新弹窗自动关旧的——全站只弹一篇。"""
    a = make_post(db, title="A")
    b = make_post(db, title="B")

    posts.set_popup(db, a, on=True)
    posts.set_popup(db, b, on=True)

    db.refresh(a)
    db.refresh(b)
    assert a.is_popup is False
    assert b.is_popup is True


def test_active_popup_respects_expiry(db):
    """过期的弹窗不再返回。"""
    a = make_post(db, title="A")
    posts.set_popup(db, a, on=True, until=datetime.now(UTC) - timedelta(hours=1))
    assert posts.active_popup(db) is None

    posts.set_popup(db, a, on=True, until=datetime.now(UTC) + timedelta(hours=1))
    assert posts.active_popup(db).id == a.id


def test_active_popup_skips_unpublished(db):
    """下架的文章不弹。"""
    a = make_post(db, title="A", published=False)
    posts.set_popup(db, a, on=True)
    assert posts.active_popup(db) is None


def test_popup_api_returns_version(client, db):
    """弹窗接口带版本号——updated_at 的 epoch。"""
    a = make_post(db, title="公告", body="大家好")
    posts.set_popup(db, a, on=True)

    r = client.get("/api/posts/popup")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == a.id
    assert data["version"] > 0

    # 没弹窗时返回 null
    posts.set_popup(db, a, on=False)
    assert client.get("/api/posts/popup").json() is None


def test_content_change_bumps_version(client, db, root_client):
    """改了内容版本号就变——确认过的人也会重新弹。"""
    a = make_post(db, title="公告", body="v1")
    posts.set_popup(db, a, on=True)
    v1 = client.get("/api/posts/popup").json()["version"]

    root_client.put(f"/api/admin/posts/{a.id}", json={"body": "v2"})
    v2 = client.get("/api/posts/popup").json()["version"]
    assert v2 > v1


# --------------------------------------------------------------------------- #
# 公开接口
# --------------------------------------------------------------------------- #


def test_list_only_published(client, db):
    """列表只给已发布的。"""
    make_post(db, title="看得见", sort_order=1)
    make_post(db, title="看不见", published=False, sort_order=2)

    rows = client.get("/api/posts").json()
    assert [r["title"] for r in rows] == ["看得见"]


def test_list_sorted_by_created_at_then_sort_order(client, db):
    """先按 sort_order（置顶），同值再按发布时间倒序。"""
    from datetime import UTC, datetime, timedelta
    base = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    # 三篇同 sort_order，created_at 递增：后发的排前面
    for i, title in enumerate(["a", "b", "c"]):
        p = make_post(db, title=title, sort_order=0)
        # 手动改 created_at 保证顺序
        p.created_at = base + timedelta(hours=i)
        db.add(p)
    db.commit()
    # 一篇 sort_order=1，不管时间多旧都排最前
    pinned = make_post(db, title="置顶", sort_order=1)
    pinned.created_at = base - timedelta(days=1)
    db.add(pinned)
    db.commit()

    rows = client.get("/api/posts").json()
    assert [r["title"] for r in rows] == ["置顶", "c", "b", "a"]


def test_detail_unpublished_is_404(client, db):
    """未发布的文章详情 404。"""
    make_post(db, title="藏起来的", slug="hidden", published=False)
    assert client.get("/api/posts/hidden").status_code == 404


def test_rss_feed(client, db):
    """RSS 是合法 XML，带文章条目。"""
    make_post(db, title="RSS文章", body="正文内容", slug="rss-post")

    r = client.get("/api/posts/rss.xml")
    assert r.status_code == 200
    assert "application/rss+xml" in r.headers["content-type"]
    assert "RSS文章" in r.text
    assert "/post/rss-post" in r.text


# --------------------------------------------------------------------------- #
# 写作权限
# --------------------------------------------------------------------------- #


def test_member_without_permission_cannot_post(member_client, db):
    """没勾「可发文章」的成员发不了。"""
    r = member_client.post("/api/admin/posts", json={"title": "x"})
    assert r.status_code == 403


def test_member_with_can_post_can_write(root_client, member_client, db):
    """勾了就能写，但只能改自己的。"""
    from app.models import User

    member = db.exec(select(User).where(User.student_id == MEMBER_STUDENT_ID)).first()
    member.can_post = True
    db.add(member)
    db.commit()

    r = member_client.post("/api/admin/posts", json={"title": "成员写的", "body": "内容"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    # 自己能改
    r = member_client.put(f"/api/admin/posts/{pid}", json={"body": "改过的"})
    assert r.status_code == 200

    # 但不能动别人写的
    other = make_post(db, title="别人的", author_id=1)
    r = member_client.put(f"/api/admin/posts/{other.id}", json={"body": "x"})
    assert r.status_code == 403

    # 也不能删（删除是管理员独有）
    r = member_client.delete(f"/api/admin/posts/{pid}")
    assert r.status_code == 403


def test_admin_can_edit_others(root_client, member_client, db):
    """管理员能改成员写的文章。"""
    from app.models import User

    member = db.exec(select(User).where(User.student_id == MEMBER_STUDENT_ID)).first()
    member.can_post = True
    db.add(member)
    db.commit()

    r = member_client.post("/api/admin/posts", json={"title": "成员的", "body": "x"})
    pid = r.json()["id"]

    r = root_client.put(f"/api/admin/posts/{pid}", json={"body": "管理员改的"})
    assert r.status_code == 200
    assert r.json()["body"] == "管理员改的"


def test_unpublish_disables_popup(root_client, db):
    """下架连着弹窗一起关。"""
    a = make_post(db, title="A")
    posts.set_popup(db, a, on=True)

    root_client.post(f"/api/admin/posts/{a.id}/publish", json={"published": False})

    db.refresh(a)
    assert a.is_popup is False
    assert a.popup_until is None
    assert client_empty_popup(db) is None


def client_empty_popup(db):
    return posts.active_popup(db)


def test_excerpt_strips_markdown(db):
    """摘要抽出来的是纯文本。"""
    post = make_post(
        db,
        title="x",
        body="## 标题\n\n**加粗** [链接](http://x) `code`\n\n| 表格 | 行 |\n|---|---|",
    )
    r = posts  # noqa
    from app.routers.posts import _excerpt

    text = _excerpt(post.body)
    assert "标题" in text
    assert "加粗" in text
    assert "链接" in text
    assert "**" not in text
    assert "|" not in text


def test_update_user_can_grant_post_permission(root_client):
    """管理员能在成员管理里给成员开「可发文章」。"""
    created = root_client.post(
        "/api/admin/users",
        json={"student_id": "24125023099", "display_name": "队员", "password": "pw123456"},
    )
    assert created.status_code in (200, 201), created.text
    uid = created.json()["id"]

    assert created.json()["can_post"] is False

    res = root_client.patch(f"/api/admin/users/{uid}", json={"can_post": True})
    assert res.status_code == 200
    assert res.json()["can_post"] is True

    res = root_client.patch(f"/api/admin/users/{uid}", json={"can_post": False})
    assert res.status_code == 200
    assert res.json()["can_post"] is False
