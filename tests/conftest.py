"""Test-wide defaults.

Set before anything imports ``app``, because ``get_settings`` is cached and the
scrypt parameters are read at call time.
"""

from __future__ import annotations

import os

# Production uses 2**14, which costs ~50ms per hash and makes the suite crawl.
# The tests cover the logic around hashing rather than its cost; the production
# default is asserted explicitly in test_security.py.
os.environ.setdefault("SGU_ACW_SCRYPT_N", "1024")


# --------------------------------------------------------------------------- #
# 注册审核 helper
# --------------------------------------------------------------------------- #
#
# 注册与校外自助注册现在都只会建出「待审核」账号，不会下发会话，登录接口也会
# 返回 403。大多数用例关心的是通过审核之后的行为，因此提供一个 helper：
# 用管理员客户端把某人的待审注册单通过掉。
#
# 注意它**不是** autouse fixture。`test_reviews.py` 里要断言「未审核时看不到、
# 登不上」，那类用例必须拿到原始的待审核状态，自动审核会把它们变成假绿。


def approve_registration(admin_client, student_id: str) -> dict:
    """用管理员的客户端通过 ``student_id`` 的注册审核单，返回审核单内容。"""
    page = admin_client.get(
        "/api/admin/reviews", params={"status": "pending", "kind": "registration"}
    )
    assert page.status_code == 200, page.text
    for row in page.json()["items"]:
        if row["student_id"] == student_id:
            response = admin_client.post(f"/api/admin/reviews/{row['id']}/approve", json={})
            assert response.status_code == 200, response.text
            return response.json()
    raise AssertionError(f"{student_id} 没有待审核的注册申请")


def approve_all_registrations(admin_client) -> list[dict]:
    """通过当前全部待审核注册单，返回通过的审核单列表。"""
    page = admin_client.get(
        "/api/admin/reviews",
        params={"status": "pending", "kind": "registration", "page_size": 100},
    )
    assert page.status_code == 200, page.text
    approved = []
    for row in page.json()["items"]:
        response = admin_client.post(f"/api/admin/reviews/{row['id']}/approve", json={})
        assert response.status_code == 200, response.text
        approved.append(response.json())
    return approved
