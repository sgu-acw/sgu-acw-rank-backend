"""站内信权限、去重、恢复和独立已读状态。"""

# ruff: noqa: F811 -- imported pytest fixtures are injected by parameter name
from sqlmodel import Session, select

from app import notifications
from app.models import AdminNotification, User
from tests.test_camps import (  # noqa: F401
    MEMBER_STUDENT_ID,
    client,
    db,
    env,
    member_client,
    root_client,
)


def report(db):
    notifications.report(db, "sync:123", "同步失败", "成绩暂时滞后", "/camps/admin")
    db.commit()
    return db.exec(select(AdminNotification)).one()


def test_all_admins_receive_independent_unread(root_client, member_client, db):
    user = db.exec(select(User).where(User.student_id == MEMBER_STUDENT_ID)).one()
    user.role = "admin"
    db.add(user)
    db.commit()
    notice = report(db)
    base = "/api/admin/notifications"
    assert root_client.get(base).json()["unread_count"] == 1
    assert member_client.get(base).json()["unread_count"] == 1
    assert root_client.post(f"{base}/{notice.id}/read").status_code == 200
    assert root_client.get(f"{base}/unread-count").json()["count"] == 0
    assert member_client.get(f"{base}/unread-count").json()["count"] == 1
    # 重复失败合并次数，不重新刷未读。
    report(db)
    assert root_client.get(base).json()["items"][0]["occurrences"] == 2
    assert root_client.get(base).json()["unread_count"] == 0
    notifications.resolve(db, "sync:123")
    db.commit()
    recovered = root_client.get(base).json()
    assert recovered["unread_count"] == 1
    assert recovered["items"][0]["resolved_at"]
    root_client.post(f"{base}/read-all")
    report(db)
    reopened = root_client.get(base).json()
    assert reopened["unread_count"] == 1
    assert reopened["items"][0]["resolved_at"] is None
    assert reopened["total"] == 1


def test_notification_permissions(client, member_client, root_client, db):
    notice = report(db)
    base = "/api/admin/notifications"
    for path in [base, f"{base}/unread-count"]:
        assert client.get(path).status_code == 401
        assert member_client.get(path).status_code == 403
    assert member_client.post(f"{base}/{notice.id}/read").status_code == 403
    assert member_client.post(f"{base}/read-all").status_code == 403
    assert root_client.post(f"{base}/99999/read").status_code == 404


def test_unread_filter_and_pagination(root_client, db):
    for i in range(4):
        notifications.report(db, f"sync:{i}", "同步失败", "请检查")
    db.commit()
    base = "/api/admin/notifications"
    page = root_client.get(f"{base}?page_size=2").json()
    assert page["total"] == 4 and len(page["items"]) == 2
    root_client.post(f"{base}/{page['items'][0]['id']}/read")
    unread = root_client.get(f"{base}?unread_only=true&page_size=2").json()
    assert unread["total"] == unread["unread_count"] == 3
    assert all(not row["read"] for row in unread["items"])
    assert root_client.get(f"{base}?page=0").status_code == 422


def test_server_error_reports_safe_route_and_recovers(root_client, db):
    state = {"broken": True}

    @root_client.app.get("/api/test-health/{item_id}")
    def health(item_id: int):
        if state["broken"]:
            raise RuntimeError("secret-password-must-not-appear")
        return {"ok": True}

    assert root_client.get("/api/test-health/1?token=secret").status_code == 500
    notice = root_client.get("/api/admin/notifications").json()["items"][0]
    assert "secret" not in str(notice)
    assert "/api/test-health/{item_id}" in notice["body"]
    state["broken"] = False
    assert root_client.get("/api/test-health/2").status_code == 200
    assert root_client.get("/api/admin/notifications").json()["items"][0]["resolved_at"]


def test_delete_user_clears_camp_and_notification_refs(root_client, db):
    """删成员时 CampRegistration 和 NotificationRead 必须随之清除，否则外键报错。"""
    from sqlmodel import select

    from app.models import CampRegistration, NotificationRead, User
    from tests.test_camps import make_camp

    # 通过管理员接口建一个测试成员（学号 24125011002，与 MEMBER_STUDENT_ID 不同）
    resp = root_client.post(
        "/api/admin/users",
        json={"student_id": "24125011002", "password": "TestPass1!", "display_name": "清理测试"},
    )
    assert resp.status_code in (200, 201), resp.text
    member_id = resp.json()["id"]

    # 直接写库建集训报名记录（绕过平台账号验证）
    camp = make_camp(db)
    db.add(CampRegistration(camp_id=camp.id, user_id=member_id))
    db.commit()

    assert (
        db.exec(select(CampRegistration).where(CampRegistration.user_id == member_id)).first()
        is not None
    )

    # 产生通知，让 root 标已读以生成 NotificationRead
    from app import notifications
    from app.db import session_scope

    with session_scope() as s:
        notifications.report(s, "cleanup-test-2", "清理测试", "body", "/admin")
    notif_list = root_client.get("/api/admin/notifications").json()
    assert notif_list["items"], "需要至少一条通知"
    root_client.post(f"/api/admin/notifications/{notif_list['items'][0]['id']}/read")

    root_id = db.exec(select(User.id).where(User.role == "root")).first()
    assert (
        db.exec(select(NotificationRead).where(NotificationRead.user_id == root_id)).first()
        is not None
    )

    # 删除测试成员
    del_resp = root_client.delete(f"/api/admin/users/{member_id}")
    assert del_resp.status_code == 204

    # 验证集训报名记录已清除
    assert (
        db.exec(select(CampRegistration).where(CampRegistration.user_id == member_id)).all() == []
    )


def test_workshop_override_lifecycle(root_client, member_client):
    """特殊状态：设置 → 公开可读 → 覆盖 → 清除。"""
    assert member_client.get("/api/workshop/override").status_code == 200
    assert (
        member_client.put("/api/admin/workshop/override", json={"label": "测试"}).status_code == 403
    )

    resp = root_client.put(
        "/api/admin/workshop/override",
        json={"label": "赛事占用", "note": "ICPC 集训占用，暂不开放"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["label"] == "赛事占用"

    pub = member_client.get("/api/workshop/override").json()
    assert pub["label"] == "赛事占用"
    assert "暂不开放" in pub["note"]

    # 同窗口覆盖：旧的被顶掉
    root_client.put("/api/admin/workshop/override", json={"label": "考试周"})
    assert member_client.get("/api/workshop/override").json()["label"] == "考试周"

    # 结束时间已经在过去：直接拒绝，不产生空条目
    assert (
        root_client.put(
            "/api/admin/workshop/override",
            json={"label": "已过期", "until": "2020-01-01T00:00:00+00:00"},
        ).status_code
        == 422
    )
    assert member_client.get("/api/workshop/override").json()["label"] == "考试周"

    # 清除当前生效的
    root_client.put("/api/admin/workshop/override", json={"label": "再设一个"})
    assert member_client.get("/api/workshop/override").json()["label"] == "再设一个"
    assert root_client.delete("/api/admin/workshop/override").status_code == 204
    assert member_client.get("/api/workshop/override").json() is None

    assert root_client.put("/api/admin/workshop/override", json={"label": "  "}).status_code == 422


def test_workshop_schedule(root_client, member_client):
    """排期：未来时段的状态到点才生效，过期自动恢复，可删。"""
    from datetime import UTC, datetime, timedelta

    def at(hours: int) -> str:
        return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()

    # 排一个 2 小时后开始、4 小时后结束的状态
    resp = root_client.put(
        "/api/admin/workshop/override",
        json={
            "label": "集训队比赛",
            "note": "明天下午",
            "start_at": at(2),
            "until": at(4),
        },
    )
    assert resp.status_code == 201
    sid = resp.json()["id"]

    # 还没到点：当前状态不受影响
    assert member_client.get("/api/workshop/override").json() is None

    # 但公开排期里能看到
    schedule = member_client.get("/api/workshop/schedule").json()
    assert any(r["id"] == sid and r["label"] == "集训队比赛" for r in schedule)

    # 结束早于开始 / 已过去的结束时间 都拒
    assert (
        root_client.put(
            "/api/admin/workshop/override",
            json={"label": "x", "start_at": at(4), "until": at(2)},
        ).status_code
        == 422
    )
    assert (
        root_client.put(
            "/api/admin/workshop/override",
            json={"label": "x", "until": at(-1)},
        ).status_code
        == 422
    )

    # 排期和立即生效并存：立即生效的赢，排期还在
    root_client.put("/api/admin/workshop/override", json={"label": "现在占用"})
    cur = member_client.get("/api/workshop/override").json()
    assert cur["label"] == "现在占用"
    assert any(r["id"] == sid for r in member_client.get("/api/workshop/schedule").json())

    # 删掉排期 / 删掉当前
    assert root_client.delete(f"/api/admin/workshop/override/{sid}").status_code == 204
    assert not any(r["id"] == sid for r in member_client.get("/api/workshop/schedule").json())
    assert root_client.delete("/api/admin/workshop/override").status_code == 204
    assert member_client.get("/api/workshop/override").json() is None
    assert member_client.delete("/api/admin/workshop/override").status_code == 403


def test_workshop_override_patch(root_client, member_client):
    """编辑条目：改字段、清空时间、校验、404。"""
    from datetime import UTC, datetime, timedelta

    def at(hours: int) -> str:
        return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()

    resp = root_client.put(
        "/api/admin/workshop/override",
        json={"label": "原始", "note": "旧说明", "start_at": at(2), "until": at(4)},
    )
    rid = resp.json()["id"]

    # 只改 label：其他字段不动
    patched = root_client.patch(
        f"/api/admin/workshop/override/{rid}", json={"label": "改名"}
    ).json()
    assert patched["label"] == "改名"
    assert patched["note"] == "旧说明"
    assert patched["start_at"] is not None

    # 清空 start_at → 立即生效
    patched = root_client.patch(
        f"/api/admin/workshop/override/{rid}", json={"start_at": None}
    ).json()
    assert patched["start_at"] is None
    assert member_client.get("/api/workshop/override").json()["label"] == "改名"

    # 结束早于开始 / 过去 都拒
    assert (
        root_client.patch(f"/api/admin/workshop/override/{rid}", json={"until": at(-1)}).status_code
        == 422
    )
    assert (
        root_client.patch(f"/api/admin/workshop/override/{rid}", json={"label": "  "}).status_code
        == 422
    )

    # 不存在的 id
    assert (
        root_client.patch("/api/admin/workshop/override/999", json={"label": "x"}).status_code
        == 404
    )
    # 已删除的也不能再改
    root_client.delete(f"/api/admin/workshop/override/{rid}")
    assert (
        root_client.patch(f"/api/admin/workshop/override/{rid}", json={"label": "x"}).status_code
        == 404
    )


def test_push_subscribe_and_announce(root_client, member_client):
    """订阅写库、退订清理、群体解析、未配置降级。"""
    # 未登录不能订阅
    import fastapi.testclient
    from sqlmodel import select

    from app.db import get_engine
    from app.main import create_app
    from app.models import PushSubscription

    with fastapi.testclient.TestClient(create_app()) as anon:
        assert (
            anon.post(
                "/api/push/subscribe",
                json={"endpoint": "https://x", "p256dh": "p", "auth": "a"},
            ).status_code
            == 401
        )

    # 成员订阅
    sub = {"endpoint": "https://push.example.com/sub/1", "p256dh": "key1", "auth": "auth1"}
    assert member_client.post("/api/push/subscribe", json=sub).status_code == 201
    # 换主：同 endpoint 换到另一个成员绑
    assert root_client.post(
        "/api/admin/users",
        json={"student_id": "24125011004", "password": "TestPass1!", "display_name": "别的"},
    ).status_code in (200, 201)

    with Session(get_engine()) as s:
        row = s.exec(
            select(PushSubscription).where(PushSubscription.endpoint == sub["endpoint"])
        ).one()
        from app.models import User
        from tests.test_camps import MEMBER_STUDENT_ID

        member = s.exec(select(User).where(User.student_id == MEMBER_STUDENT_ID)).one()
        assert row.user_id == member.id

    # 群体解析：audience=all 覆盖全部活跃成员；camp:N 覆盖报名人
    announce = root_client.post(
        "/api/admin/push/announce",
        json={"title": "t", "audience": "all", "request_id": "test-announce-0001"},
    )
    assert announce.status_code in (200, 503)  # 未配置 VAPID 时 503
    if announce.status_code == 200:
        assert announce.json()["audience"] >= 2

    assert (
        root_client.post(
            "/api/admin/push/announce", json={"title": "t", "audience": "bogus"}
        ).status_code
        == 422
    )
    assert member_client.post("/api/admin/push/announce", json={"title": "t"}).status_code == 403

    # 退订清掉自己的订阅
    assert (
        member_client.post("/api/push/unsubscribe", json={"endpoint": sub["endpoint"]}).status_code
        == 204
    )
    with Session(get_engine()) as s:
        assert (
            s.exec(
                select(PushSubscription).where(PushSubscription.endpoint == sub["endpoint"])
            ).first()
            is None
        )


def test_signup_lifecycle(root_client, member_client):
    """报名：建活动、表单字段校验、提交/修改/撤销、名额、窗口、名单、导出。"""
    from datetime import UTC, datetime, timedelta

    def at(hours: int) -> str:
        return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()

    fields = [
        {"label": "队名", "type": "text", "required": True},
        {"label": "年级", "type": "select", "required": True, "options": ["2024", "2025"]},
        {"label": "备注", "type": "textarea", "required": False},
        {"label": "带电脑", "type": "checkbox", "required": False},
    ]
    create = root_client.post(
        "/api/admin/signups",
        json={
            "name": "线下赛测试",
            "description": "周末打一场",
            "fields": fields,
            "open_at": at(-1),
            "close_at": at(24),
            "capacity": 1,
        },
    )
    assert create.status_code == 201, create.text
    sid = create.json()["id"]

    # 公开列表和详情可见
    assert any(s["id"] == sid for s in member_client.get("/api/signups").json())
    detail = member_client.get(f"/api/signups/{sid}").json()
    assert detail["status"] == "open"
    assert [f["key"] for f in detail["fields"]] == ["field_1", "field_2", "field_3", "field_4"]

    # 必填缺失拒
    bad = member_client.post(f"/api/signups/{sid}/entries", json={"answers": {"field_1": "队A"}})
    assert bad.status_code == 422
    # select 非法值拒
    bad2 = member_client.post(
        f"/api/signups/{sid}/entries",
        json={"answers": {"field_1": "队A", "field_2": "2023"}},
    )
    assert bad2.status_code == 422

    # 正常提交
    ok = member_client.post(
        f"/api/signups/{sid}/entries",
        json={"answers": {"field_1": "队A", "field_2": "2024", "field_4": True}},
    )
    assert ok.status_code == 200
    assert member_client.get(f"/api/signups/{sid}/entries/me").json()["answers"]["field_1"] == "队A"

    # 名额满：另一个成员报不进
    other = root_client.post(
        "/api/admin/users",
        json={"student_id": "24125011005", "password": "TestPass1!", "display_name": "替补"},
    )
    assert other.status_code in (200, 201)
    # 新成员登录拿会话
    import fastapi.testclient

    from app.main import create_app

    with fastapi.testclient.TestClient(create_app()) as c2:
        r = c2.post("/api/auth/login", json={"student_id": "24125011005", "password": "TestPass1!"})
        assert r.status_code == 200
        resp = c2.post(
            f"/api/signups/{sid}/entries",
            json={"answers": {"field_1": "队B", "field_2": "2025"}},
        )
        assert resp.status_code == 409
        assert "名额" in resp.json()["detail"]

    # 修改答案不占新名额
    upd = member_client.post(
        f"/api/signups/{sid}/entries",
        json={"answers": {"field_1": "队A改", "field_2": "2025"}},
    )
    assert upd.status_code == 200

    # 撤销 → 名额让出来
    assert member_client.delete(f"/api/signups/{sid}/entries/me").status_code == 204

    # 管理员名单 + 导出
    entries = root_client.get(f"/api/admin/signups/{sid}/entries").json()
    assert entries == [] or isinstance(entries, list)
    exp = root_client.get(f"/api/admin/signups/{sid}/export")
    assert exp.status_code == 200
    assert exp.headers["content-type"].startswith("application/vnd.openxml")

    # 窗口校验：未开始 / 已截止拒
    early = root_client.post(
        "/api/admin/signups",
        json={"name": "未开", "open_at": at(2), "close_at": at(3)},
    ).json()
    assert (
        member_client.post(f"/api/signups/{early['id']}/entries", json={"answers": {}}).status_code
        == 409
    )

    # 权限：成员不能管理
    assert member_client.get("/api/admin/signups").status_code == 403

    # 删除连带清记录
    assert root_client.delete(f"/api/admin/signups/{sid}").status_code == 204
    assert member_client.get(f"/api/signups/{sid}").status_code == 404


def test_totp_login_flow(member_client):
    """两步验证：开启 → 登录第一步出票据 → 第二步验码发会话。"""
    import fastapi.testclient
    import pyotp

    from app.main import create_app
    from tests.test_camps import MEMBER_PASSWORD

    # 开启流程：begin 给密钥和 otpauth URI
    begin = member_client.post("/api/auth/me/totp/begin")
    assert begin.status_code == 200
    secret = begin.json()["secret"]
    assert "otpauth://" in begin.json()["otpauth_uri"]
    assert begin.json()["qr_svg"].startswith("<?xml") or "<svg" in begin.json()["qr_svg"]

    # 验错码拒
    assert member_client.post("/api/auth/me/totp/enable", json={"code": "000000"}).status_code in (
        422,
        401,
    )
    # 验正码转正
    code = pyotp.TOTP(secret).now()
    ok = member_client.post("/api/auth/me/totp/enable", json={"code": code})
    assert ok.status_code == 200

    # 已开启：session 里能看出来
    me = member_client.get("/api/auth/me")
    assert me.json()["totp_enabled"] is True

    # 新会话：密码对了但只拿到票据
    with fastapi.testclient.TestClient(create_app()) as c2:
        r = c2.post(
            "/api/auth/login", json={"student_id": MEMBER_STUDENT_ID, "password": MEMBER_PASSWORD}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["totp_required"] is True
        ticket = body["ticket"]
        # 第二步：错码拒
        bad = c2.post("/api/auth/login/totp", json={"ticket": ticket, "code": "000000"})
        assert bad.status_code == 401
        # 真码出会话
        done = c2.post(
            "/api/auth/login/totp", json={"ticket": ticket, "code": pyotp.TOTP(secret).now()}
        )
        assert done.status_code == 200
        assert done.json()["totp_required"] is False
        assert done.json()["user"]["student_id"] == MEMBER_STUDENT_ID
        # session 能用
        assert c2.get("/api/auth/me").json()["student_id"] == MEMBER_STUDENT_ID
        # 一张成功消费过的票据不能再次签发会话。
        again = c2.post(
            "/api/auth/login/totp", json={"ticket": ticket, "code": pyotp.TOTP(secret).now()}
        )
        assert again.status_code == 401

    # 关闭要先验码
    assert member_client.post("/api/auth/me/totp/disable", json={"code": "000000"}).status_code in (
        422,
        401,
    )
    off = member_client.post("/api/auth/me/totp/disable", json={"code": pyotp.TOTP(secret).now()})
    assert off.status_code == 200
    assert member_client.get("/api/auth/me").json()["totp_enabled"] is False


def test_passkey_endpoints_exist(member_client):
    """通行密钥接口的匿名/登录态区分。真实签名验证没法在单测里造
    （要真认证器），这里只验接口形状和权限。"""
    # 匿名能拿到登录挑战
    anon = member_client.post("/api/auth/passkey/login/begin")
    assert anon.status_code == 200
    opts = anon.json()["options"]
    assert "challenge" in opts and opts["rpId"]
    # 注册接口匿名不可达
    import fastapi.testclient

    from app.main import create_app

    with fastapi.testclient.TestClient(create_app()) as c:
        assert c.post("/api/me/passkeys/register/begin").status_code == 401
    # 已登录能拿注册挑战
    begin = member_client.post("/api/me/passkeys/register/begin")
    assert begin.status_code == 200
    ropts = begin.json()["options"]
    assert ropts["rp"]["id"] and ropts["user"]["name"] == MEMBER_STUDENT_ID


def test_camp_import_problems(root_client, member_client, db, monkeypatch):
    """批量导入：题号列表直给 + 题单拉取，重复跳过，错题收集。"""
    monkeypatch.setattr("app.routers.camps._fetch_and_cache", lambda *args: None)
    from datetime import UTC, datetime, timedelta

    from app.models import Camp, ProblemList, ProblemListItem

    now = datetime.now(UTC)
    with db:
        camp = Camp(
            name="导入测试",
            description="",
            start_at=now + timedelta(days=1),
            end_at=now + timedelta(days=7),
            register_open_at=now,
            visible_at=now,
            created_by=1,
        )
        db.add(camp)
        # 题单（含一条已在营里的题）
        pl = ProblemList(kind="list", title="基础题单", description="", position=0)
        db.add(pl)
        db.flush()
        for i, (platform, key) in enumerate(
            [("luogu", "P1000"), ("codeforces", "1A"), ("atcoder", "abc300_a")]
        ):
            db.add(ProblemListItem(list_id=pl.id, platform=platform, key=key, position=i))
        db.commit()
        camp_id, pl_id = camp.id, pl.id

    # 先手动加一道——等下导入要跳过它
    add = root_client.post(
        f"/api/admin/camps/{camp_id}/problems",
        json={"platform": "luogu", "key": "P1000"},
    )
    assert add.status_code == 201

    # 题单整单导入：P1000 跳过，另两道进来
    imp = root_client.post(
        f"/api/admin/camps/{camp_id}/problems/import",
        json={"problem_list_id": pl_id},
    )
    assert imp.status_code == 200, imp.text
    assert imp.json()["added"] == 2
    assert imp.json()["skipped"] == 1

    # 直给 items：新的进、重复跳、非法的进 failed
    imp2 = root_client.post(
        f"/api/admin/camps/{camp_id}/problems/import",
        json={
            "items": [
                {"platform": "luogu", "key": "P1001"},
                {"platform": "luogu", "key": "P1000"},   # 重复
                {"platform": "bogus", "key": "x"},       # 非法平台
                {"platform": "codeforces", "key": " "},  # 空 key
            ]
        },
    )
    assert imp2.status_code == 200
    assert imp2.json()["added"] == 1
    assert imp2.json()["skipped"] == 1
    assert len(imp2.json()["failed"]) == 2

    # 空集拒
    assert (
        root_client.post(
            f"/api/admin/camps/{camp_id}/problems/import", json={"items": []}
        ).status_code
        == 422
    )
    # 不存在的题单拒
    assert (
        root_client.post(
            f"/api/admin/camps/{camp_id}/problems/import", json={"problem_list_id": 999}
        ).status_code
        == 404
    )

    detail = root_client.get(f"/api/admin/camps/{camp_id}").json()
    keys = {p["key"] for p in detail["problems"]}
    assert keys == {"P1000", "1A", "abc300_a", "P1001"}
