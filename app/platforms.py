"""读取各平台公开资料，用于绑定时的签名校验与后续抓取。

四个平台的差异（均已实测）：

============  ==================  ====================  ==============
平台           资料字段             读取方式               需要登录
============  ==================  ====================  ==============
Codeforces    organization        官方 API              否
AtCoder       Affiliation         用户页 HTML           否
洛谷           slogan              用户页内嵌 JSON        否（需反爬 cookie）
牛客           coder-brief         用户页 HTML           否
============  ==================  ====================  ==============

洛谷那个「反爬 cookie」不是登录凭据：它只需要一个随机的 ``__client_id``，
服务端用它做频次统计。第一次请求会返回 302 并下发 ``C3VK``，带上它重试即可，
所以这里统一做一次重试。

解题记录的可得性并不一致，且与资料字段无关：

* CF、AtCoder、牛客都带精确时间戳，能直接算出每天过了几题。
* 洛谷只给「累计通过哪些题」的集合，没有任何时间信息，只能靠快照差分。

牛客容易被误判成拿不到：它的**比赛**记录（rating-history）确实没有逐题
数据，但**练习**记录（practice-coding）是公开且完整的，服务端直出表格，
带提交时间和判题结果。别把前者当成全部。
"""

from __future__ import annotations

import html
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .models import Platform

logger = logging.getLogger("sgu_acw.platforms")

#: 主站接口专用的 User-Agent。
#:
#: 不能用外层那个完整的 Chrome UA——阿里云 WAF 会把它当成浏览器流量拦下来，
#: 返回一个 HTML 挑战页（HTTP 200，内容是网页）。用这个短写法反而放行。
_NOWCODER_API_UA = "Mozilla/5.0"

#: 统计口径按东八区。牛客页面上的时间没有时区标记，本身就是北京时间。
CST = timezone(timedelta(hours=8))

#: 浏览器 UA。几个平台都会拒绝空 UA 或明显的脚本 UA。
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=8.0)


class FetchError(Exception):
    """读不到资料。``not_found`` 用来区分「账号不存在」和「网络/解析问题」。

    ``kind`` 是配置类错误：账号本身在，但它当前不可统计（隐私保护、未实名）。
    这类错误重试没有意义，同步层把它们记为「账号挂了标记」，而不是失败。
    """

    def __init__(
        self, message: str, *, not_found: bool = False, kind: str | None = None
    ) -> None:
        super().__init__(message)
        self.not_found = not_found
        self.kind = kind


@dataclass(slots=True)
class Profile:
    """从平台读到的一份资料。"""

    platform: str
    identifier: str
    #: 用于校验签名的文本。平台没有这个字段时为 None。
    signature: str | None = None
    display_name: str | None = None
    rating: int | None = None
    max_rating: int | None = None
    #: 平台内部的数字 id（洛谷/牛客用得上），拿不到则为 None。
    internal_id: str | None = None


def _clean(text: str) -> str:
    """去掉标签、反转义实体、压平空白。"""
    without_tags = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


# --------------------------------------------------------------------------- #
# Codeforces
# --------------------------------------------------------------------------- #


def fetch_codeforces(client: httpx.Client, handle: str) -> Profile:
    response = client.get(
        "https://codeforces.com/api/user.info",
        params={"handles": handle},
    )
    if response.status_code != 200:
        raise FetchError(f"Codeforces 返回 {response.status_code}")
    payload = response.json()
    if payload.get("status") != "OK":
        comment = payload.get("comment", "")
        # 官方对不存在的 handle 回 FAILED，comment 里带 "not found"。
        not_found = "not found" in comment.lower()
        raise FetchError(f"Codeforces: {comment or '查询失败'}", not_found=not_found)

    user = payload["result"][0]
    return Profile(
        platform=Platform.CODEFORCES,
        identifier=user["handle"],
        # 不能用 organization：CF 的那个字段是个选择器，会要求从已有组织里选
        # 或者新建一个，填不进任意文本。改成用几个自由文本字段，其中任意一个
        # 含验证码都算通过。
        #
        # 多个字段是因为它们各有各的用途：有人拿 lastName 放真姓氏，有人用
        # city。全算上就不会强迫成员改掉真实信息。
        signature="\n".join(
            filter(
                None,
                [
                    user.get("firstName"),
                    user.get("lastName"),
                    user.get("city"),
                    user.get("organization"),
                ],
            )
        ),
        display_name=user.get("handle"),
        rating=user.get("rating"),
        max_rating=user.get("maxRating"),
    )


# --------------------------------------------------------------------------- #
# AtCoder
# --------------------------------------------------------------------------- #


def fetch_atcoder(client: httpx.Client, handle: str) -> Profile:
    response = client.get(f"https://atcoder.jp/users/{handle}")
    if response.status_code == 404:
        raise FetchError("AtCoder 上没有这个用户", not_found=True)
    if response.status_code != 200:
        raise FetchError(f"AtCoder 返回 {response.status_code}")

    page = response.text
    # 资料表是 <th class="no-break">标题</th><td ...>值</td> 的形式。
    affiliation = re.search(r"<th[^>]*>\s*Affiliation\s*</th>\s*<td[^>]*>(.*?)</td>", page, re.S)
    rating = re.search(r"<th[^>]*>\s*Rating\s*</th>\s*<td[^>]*>(.*?)</td>", page, re.S)
    highest = re.search(r"<th[^>]*>\s*Highest Rating\s*</th>\s*<td[^>]*>(.*?)</td>", page, re.S)

    def number(match: re.Match[str] | None) -> int | None:
        if not match:
            return None
        digits = re.search(r"\d+", _clean(match.group(1)).replace(",", ""))
        return int(digits.group()) if digits else None

    return Profile(
        platform=Platform.ATCODER,
        identifier=handle,
        signature=_clean(affiliation.group(1)) if affiliation else "",
        display_name=handle,
        rating=number(rating),
        max_rating=number(highest),
    )


# --------------------------------------------------------------------------- #
# 洛谷
# --------------------------------------------------------------------------- #


def _luogu_get(client: httpx.Client, url: str) -> str:
    """带反爬握手的一次 GET。

    洛谷会先回 302 并下发 C3VK，带上它再请求才拿到内容。这里随机生成一个
    __client_id：官方把频次限制绑在这个值上，随机化可以避免多个成员共用
    同一个额度，也不需要任何真实账号。
    """
    cookie = {"__client_id": secrets.token_hex(20)}
    first = client.get(url, cookies=cookie)
    if first.status_code == 200:
        return first.text

    # 把服务端下发的 cookie 合并进来重试一次。
    merged = dict(cookie)
    merged.update({k: v for k, v in first.cookies.items()})
    second = client.get(url, cookies=merged)
    if second.status_code == 404:
        raise FetchError("洛谷上没有这个用户", not_found=True)
    if second.status_code != 200:
        message = _luogu_error(second)
        # 未实名是配置问题：账号在，但平台不给统计数据，重试没意义。
        kind = "unverified" if "实名" in message else None
        raise FetchError(message, kind=kind)
    return second.text


def _luogu_error(response: httpx.Response) -> str:
    """把洛谷的错误响应翻成人话。

    洛谷出错时返回的是结构化 JSON，里面带着自己的说明：

        {"status": 403, "data": {"errorMessage": "该用户未通过实名认证"}}

    直接报「洛谷返回 403」等于没说——用户不知道该去实名认证，只会以为是
    我们这边坏了。所以优先把洛谷的原话透传出来。

    这类 403 不是反爬：反爬会返回挑战页面（HTML），而这个是 JSON。
    """

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - 不是 JSON 就走下面的兜底
        payload = None

    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            message = data.get("errorMessage")
            if isinstance(message, str) and message.strip():
                return f"洛谷：{message.strip()}"

    return f"洛谷返回 {response.status_code}"


def _luogu_context(page: str) -> dict:
    """取出用户页内嵌的 lentille-context JSON。"""
    import json

    match = re.search(
        r'<script id="lentille-context" type="application/json">(.*?)</script>',
        page,
        re.S,
    )
    if not match:
        raise FetchError("洛谷页面结构变了，取不到数据")
    try:
        return json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError as exc:
        raise FetchError("洛谷返回的数据无法解析") from exc


def resolve_luogu_uid(client: httpx.Client, name: str) -> tuple[str, str] | None:
    """按用户名搜出 (uid, name)。搜不到返回 None。"""
    page = _luogu_get(client, f"https://www.luogu.com.cn/user/search?keyword={name}")
    import json

    try:
        payload = json.loads(page)
    except json.JSONDecodeError:
        return None
    for user in payload.get("users", []):
        if user.get("name", "").lower() == name.lower():
            return str(user["uid"]), user["name"]
    return None


def _luogu_user(client: httpx.Client, identifier: str) -> tuple[dict, dict]:
    """读洛谷用户页，返回 ``(user, data)``。读不到时 user 是空 dict。

    两个都返回是因为 rating 不在 user 里，而在同一层的 elo 字段里。
    """
    page = _luogu_get(client, f"https://www.luogu.com.cn/user/{identifier}")
    data = _luogu_context(page).get("data") or {}
    user = data.get("user")
    return (user if isinstance(user, dict) else {}), data


def fetch_luogu(client: httpx.Client, identifier: str) -> Profile:
    """``identifier`` 可以是数字 uid，也可以是用户名。"""
    if not identifier.isdigit():
        found = resolve_luogu_uid(client, identifier)
        if found is None:
            raise FetchError("洛谷上没有这个用户", not_found=True)
        identifier = found[0]

    user, data = _luogu_user(client, identifier)

    if not user and identifier.isdigit():
        # 数字 uid 查不到。可能是把用户名当成了 uid（比如昵称恰好全是数字），
        # 也可能就是写错了。再当用户名搜一次，搜到就用。
        #
        # 不做这一步的话，成员看到的是「洛谷上没有这个用户」，而他明明有
        # 账号——只是填的是昵称。
        found = resolve_luogu_uid(client, identifier)
        if found is not None:
            identifier = found[0]
            user, data = _luogu_user(client, identifier)

    if not user:
        raise FetchError("洛谷上没有这个用户", not_found=True)

    # 洛谷用户页本身不带 rating，等级分在 elo 里。个人中心页才有完整数据，
    # 但它只在登录后可读，所以这里退一步：拿不到就算了，不影响榜单排序
    # （没 rating 的排最后）。
    rating = None
    elo = data.get("elo")
    if isinstance(elo, list) and elo:
        latest = elo[-1]
        if isinstance(latest, dict):
            value = latest.get("rating")
            rating = int(value) if isinstance(value, (int, float)) else None

    return Profile(
        platform=Platform.LUOGU,
        identifier=str(user.get("uid") or identifier),
        # 洛谷把个人简介放在 slogan（签名）和 introduction（简介）两处，
        # 签名是主页上显示的那一行，验证时两处都认。
        signature="\n".join(filter(None, [user.get("slogan"), user.get("introduction")])),
        display_name=user.get("name"),
        rating=rating,
        internal_id=str(user.get("uid") or identifier),
    )


# --------------------------------------------------------------------------- #
# 牛客
# --------------------------------------------------------------------------- #


def resolve_nowcoder_uid(client: httpx.Client, name: str) -> str | None:
    """按昵称搜出牛客 uid。搜不到返回 None。

    牛客的资料页只认数字 uid，成员却常常记得自己的昵称。搜索页是服务端
    直出的，结果里带着 ``/users/{uid}`` 的链接。

    搜到多个时取第一个——牛客的排序把最匹配的放前面。这不是万无一失，
    但比「直接报错说必须填数字」有用得多：拿错 uid 之后验证那一步会失败，
    成员会看到「签名里找不到验证码」，那时他就知道自己填错了。
    """
    page = client.get(
        "https://www.nowcoder.com/search/user",
        params={"query": name},
    )
    if page.status_code != 200:
        return None

    # 卡片结构是：href="/users/{uid}" ... <span class="nickname">昵称</span>
    #
    # 必须要求昵称完全相等。搜索是模糊匹配，「不存在的名字xyz」会匹配到
    # 「响亮的名字xyz」——把别人绑上来比报错更糟。
    for card in re.finditer(
        r'href="/users/(\d+)"[^>]*>(.{0,2000}?)</a>',
        page.text,
        re.S,
    ):
        uid, block = card.group(1), card.group(2)
        nick = re.search(r'class="nickname"[^>]*>([^<]+)<', block)
        if nick and html.unescape(nick.group(1)).strip() == name:
            return uid

    return None


def fetch_nowcoder(client: httpx.Client, identifier: str) -> Profile:
    """``identifier`` 是牛客的 uid，也接受昵称。"""
    if not identifier.isdigit():
        # 成员填了昵称。先搜出 uid 再继续，省得他对着「必须填数字」发愣。
        resolved = resolve_nowcoder_uid(client, identifier)
        if resolved is None:
            raise FetchError(
                "牛客上没有这个用户，也没搜到这个昵称。请填个人主页地址里的数字 uid。",
                not_found=True,
            )
        identifier = resolved

    response = client.get(f"https://ac.nowcoder.com/acm/contest/profile/{identifier}")
    if response.status_code != 200:
        raise FetchError(f"牛客返回 {response.status_code}")

    page = response.text
    brief = re.search(r'<div class="coder-brief">(.*?)</div>', page, re.S)
    if brief is None:
        # 页面能打开但没有这个区块，说明 uid 不存在。
        raise FetchError("牛客上没有这个用户", not_found=True)

    # 昵称不在可见文本里，而是藏在「私信」链接的 data-receiver-name 上。
    nickname = re.search(r'data-receiver-name="([^"]+)"', page)

    rating = None
    rating_match = re.search(r"(\d+)\s*Rating", _clean(page))
    if rating_match:
        rating = int(rating_match.group(1))

    return Profile(
        platform=Platform.NOWCODER,
        identifier=identifier,
        signature=_clean(brief.group(1)),
        display_name=nickname.group(1) if nickname else identifier,
        rating=rating,
        internal_id=identifier,
    )


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #

_FETCHERS = {
    Platform.CODEFORCES: fetch_codeforces,
    Platform.ATCODER: fetch_atcoder,
    Platform.LUOGU: fetch_luogu,
    Platform.NOWCODER: fetch_nowcoder,
}


def fetch_profile(platform: str, identifier: str) -> Profile:
    """读一个平台的资料。异常统一是 :class:`FetchError`。"""
    fetcher = _FETCHERS.get(platform)
    if fetcher is None:
        raise FetchError(f"不支持的平台：{platform}")

    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    try:
        with httpx.Client(
            headers=headers, timeout=DEFAULT_TIMEOUT, follow_redirects=False
        ) as client:
            return fetcher(client, identifier)
    except httpx.TimeoutException as exc:
        raise FetchError("请求超时，稍后重试") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"网络错误：{exc}") from exc


def check_signature(platform: str, identifier: str, token: str) -> tuple[bool, str]:
    """校验 ``token`` 是否出现在该账号的签名里。

    返回 ``(是否通过, 说明)``。用「包含」而不是「相等」：成员的签名里往往
    本来就有别的内容，不该要求他们清空。
    """
    try:
        profile = fetch_profile(platform, identifier)
    except FetchError as exc:
        return False, str(exc)

    signature = profile.signature or ""
    if not signature:
        label = Platform.LABELS.get(platform, platform)
        return False, f"{label} 上没有找到签名字段，请先填写再验证"

    # 大小写不敏感：成员可能把 ACW-xxxx 抄成小写。
    if token.lower() in signature.lower():
        return True, "验证通过"
    return False, "签名里没有找到验证码，请确认已保存"


# --------------------------------------------------------------------------- #
# 已过题目集合（用于每日练习量统计）
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SolvedSet:
    """一个账号做过的题目。

    ``problems`` 是 题目标识 → 首次通过时间（epoch 秒）。时间未知时为 0，
    洛谷就属于这种：它只给出累计通过的题目集合，没有任何时间信息。
    """

    platform: str
    identifier: str
    problems: dict[str, int]


def fetch_solved_codeforces(client: httpx.Client, handle: str) -> SolvedSet:
    """拉全量提交，取每题最早的 AC 时间。"""
    response = client.get(
        "https://codeforces.com/api/user.status",
        params={"handle": handle, "from": 1, "count": 100000},
    )
    if response.status_code != 200:
        raise FetchError(f"Codeforces 返回 {response.status_code}")
    payload = response.json()
    if payload.get("status") != "OK":
        comment = payload.get("comment", "")
        raise FetchError(
            f"Codeforces: {comment or '查询失败'}",
            not_found="not found" in comment.lower(),
        )

    problems: dict[str, int] = {}
    for submission in payload["result"]:
        if submission.get("verdict") != "OK":
            continue
        problem = submission.get("problem") or {}
        contest_id = problem.get("contestId")
        index = problem.get("index")
        if contest_id is None or index is None:
            # 一些题（如 acmsguru）没有 contestId，用名字兜底。
            key = f"name:{problem.get('name', '')}"
        else:
            key = f"{contest_id}{index}"
        created = int(submission.get("creationTimeSeconds") or 0)
        # 同一题可能多次 AC，只记最早那次。
        if key not in problems or (created and created < problems[key]):
            problems[key] = created

    return SolvedSet(platform=Platform.CODEFORCES, identifier=handle, problems=problems)


def fetch_solved_atcoder(client: httpx.Client, handle: str) -> SolvedSet:
    """从 kenkoooo 的镜像拉提交，取每题最早的 AC 时间。"""
    response = client.get(
        "https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions",
        params={"user": handle, "from_second": 0},
    )
    if response.status_code != 200:
        raise FetchError(f"AtCoder 数据源返回 {response.status_code}")
    submissions = response.json()
    if not isinstance(submissions, list):
        raise FetchError("AtCoder 数据源返回了意外的内容")

    problems: dict[str, int] = {}
    for submission in submissions:
        if submission.get("result") != "AC":
            continue
        problem_id = submission.get("problem_id")
        if not problem_id:
            continue
        epoch = int(submission.get("epoch_second") or 0)
        if problem_id not in problems or (epoch and epoch < problems[problem_id]):
            problems[problem_id] = epoch

    return SolvedSet(platform=Platform.ATCODER, identifier=handle, problems=problems)


def fetch_solved_luogu(client: httpx.Client, identifier: str) -> SolvedSet:
    """洛谷只给累计通过集合，时间一律为 0，靠快照差分出每日增量。"""
    if not identifier.isdigit():
        found = resolve_luogu_uid(client, identifier)
        if found is None:
            raise FetchError("洛谷上没有这个用户", not_found=True)
        identifier = found[0]

    page = _luogu_get(client, f"https://www.luogu.com.cn/user/{identifier}/practice")
    data = (_luogu_context(page).get("data")) or {}
    if data.get("privacy") is True:
        # 开了隐私保护的账号接口直接给空列表。这不是抓取失败，是账号
        # 自身的配置状态——打标记提示本人关掉，不进入失败重试。
        raise FetchError("该用户开启了洛谷隐私保护，请关闭后再同步", kind="privacy")
    passed = data.get("passed")
    if passed is None:
        # privacy=True 的用户会返回空列表，这与「一题没做」无法区分。
        raise FetchError("该用户的练习记录未公开，无法统计")
    if not isinstance(passed, list):
        raise FetchError("洛谷返回了意外的练习数据")

    problems = {str(item.get("pid")): 0 for item in passed if item.get("pid")}
    return SolvedSet(platform=Platform.LUOGU, identifier=identifier, problems=problems)


def fetch_solved_nowcoder(client: httpx.Client, identifier: str) -> SolvedSet:
    """牛客的过题记录。``identifier`` 是数字 uid。

    牛客有两套记录，**分开计数**，要合并起来才是完整的：

    * **竞赛站**（ac.nowcoder.com）的练习记录。页面服务端直出表格，带精确
      时间与判题结果。题目只有标题，没有题号。
    * **主站**（www.nowcoder.com）的提交历史。JSON 接口，有题号、题名和
      毫秒时间戳。题号形如 ``noob29``、``ACM342``、``BISHI74``。

    两边几乎不重合——实测某用户主站 88 题、竞赛站 192 题，交集只有 1 题。
    所以只抓一边会漏掉一大块。

    两套的题目用不同前缀区分，否则同名题会互相覆盖。
    """
    if not identifier.isdigit():
        raise FetchError("牛客需要填数字 uid")

    problems: dict[str, int] = {}

    # 竞赛站。抓不到不该让整个同步失败——主站那部分往往还有数据。
    contest_error: FetchError | None = None
    try:
        problems.update(_nowcoder_contest_solved(client, identifier))
    except FetchError as exc:
        contest_error = exc

    # 主站。同样，失败时保留竞赛站已经拿到的。
    main_error: FetchError | None = None
    try:
        problems.update(_nowcoder_main_solved(client, identifier))
    except FetchError as exc:
        main_error = exc

    if not problems and (contest_error or main_error):
        # 没有任何一边拿到 AC，且至少一路请求失败时，不能把未知状态当成零题。
        raise contest_error or main_error

    # 一边成功一边失败是常见情况（限流、平台改版）。这时候**必须留痕**：
    # 早先静默吞掉，结果快照里少了一半数据而没有任何提示，很难查。
    if contest_error or main_error:
        failed = "竞赛站" if contest_error else "主站"
        detail = contest_error or main_error
        logger.warning(
            "牛客 %s 抓取失败（%s），本次只统计到 %d 题：%s",
            failed,
            identifier,
            len(problems),
            detail,
        )

    return SolvedSet(platform=Platform.NOWCODER, identifier=identifier, problems=problems)


def _nowcoder_contest_solved(client: httpx.Client, identifier: str) -> dict[str, int]:
    """竞赛站的练习记录。

    题目用标题当键——页面上的链接只有题目 id，而同一题在不同比赛里 id 不同，
    标题反而更稳定。
    """
    problems: dict[str, int] = {}
    page = 1
    page_size = 100
    # 翻页上限，防止接口行为异常时无限循环。100 页 = 一万条提交，够用。
    max_pages = 100

    while page <= max_pages:
        response = client.get(
            f"https://ac.nowcoder.com/acm/contest/profile/{identifier}/practice-coding",
            params={"page": page, "pageSize": page_size},
        )
        if response.status_code != 200:
            if page == 1:
                raise FetchError(f"牛客竞赛站返回 {response.status_code}")
            break

        rows = _nowcoder_rows(response.text)
        if not rows:
            # 页面能开但没有任何记录：uid 可能不存在，也可能只是没提交过。
            # 无法区分，按「没有数据」处理而不报错。
            break

        for row in rows:
            if row.verdict != "答案正确":
                continue
            key = f"nowcoder:contest:{row.problem_id}"
            epoch = _parse_nowcoder_time(row.submitted_at)
            if epoch is None:
                continue
            if key not in problems or epoch < problems[key]:
                problems[key] = epoch

        # 本页不足一页，说明到底了。
        if len(rows) < page_size:
            break
        page += 1

    return problems


def _nowcoder_main_solved(client: httpx.Client, identifier: str) -> dict[str, int]:
    """主站的提交历史。

    接口在 gw-c 子域上，是 POST。``pageSize`` 一次最多 100，比竞赛站页面
    翻页省事得多。

    题号（``questionNum``）唯一对应一道题，所以用它当键，而不是标题——
    标题可能带全角空格之类，也可能会改。
    """
    problems: dict[str, int] = {}
    page = 1
    page_size = 100
    max_pages = 100

    while page <= max_pages:
        response = client.post(
            "https://gw-c.nowcoder.com/api/sparta/user/question-training/submission-history",
            json={"userId": int(identifier), "pageNo": page, "pageSize": page_size},
            headers={
                "Origin": "https://www.nowcoder.com",
                "Referer": "https://www.nowcoder.com/",
                # UA 必须覆盖成这个短写法。用外层那个完整的 Chrome UA 会被
                # 阿里云 WAF 拦下，返回一个 HTML 挑战页（HTTP 200，但内容是
                # 网页不是 JSON），json() 就抛异常。
                # 反直觉：越像浏览器越容易被拦。
                "User-Agent": _NOWCODER_API_UA,
            },
        )
        if response.status_code != 200:
            if page == 1:
                raise FetchError(f"牛客主站返回 {response.status_code}")
            break

        # 302 要跟着走。外层 client 是 follow_redirects=False（绑定校验时
        # 需要看到 302 才能判断），但主站这个接口会先回一次跳转，
        # 不跟的话拿到的是空 body，json() 直接抛异常。
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise FetchError("牛客主站返回了跳转但没给目标地址")
            response = client.get(location)
            if response.status_code != 200:
                raise FetchError(f"牛客主站跳转后返回 {response.status_code}")

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise FetchError(
                f"牛客主站返回了无法解析的内容（HTTP {response.status_code}）"
            ) from exc

        if not payload.get("success"):
            if page == 1:
                raise FetchError(f"牛客主站：{payload.get('msg') or '查询失败'}")
            break

        data = payload.get("data") or {}
        records = data.get("records") or []
        if not records:
            break

        for record in records:
            if not record.get("accept"):
                continue
            problem = record.get("problem") or {}
            # 用 questionUuid 当键，不是 questionId：uuid 能拼出题面地址
            # （/questionTerminal/{uuid}），这样才抓得到题目名。
            # questionId 是个纯数字，拼不出任何可访问的地址。
            problem_uuid = problem.get("questionUuid")
            if not problem_uuid:
                continue
            submission = record.get("submission") or {}
            # 毫秒时间戳。取最早一次通过，与竞赛站那边的口径一致。
            epoch = submission.get("createdDate")
            if not isinstance(epoch, (int, float)):
                continue
            epoch = int(epoch // 1000)

            key = f"nowcoder:main:{problem_uuid}"
            if key not in problems or epoch < problems[key]:
                problems[key] = epoch

        # totalPage 是后端算的，但实测会随 pageSize 变化，所以按实际条数判断。
        if len(records) < page_size:
            break
        page += 1

    return problems


#: 牛客表格列：运行ID、题目、运行结果、得分、运行时间、使用内存、代码长度、语言、提交时间。
_NOWCODER_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_NOWCODER_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_NOWCODER_TIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})")


@dataclass
class NowcoderRow:
    """竞赛站练习记录的一行。"""

    run_id: str
    #: 题目 id，来自题目链接的 ``/acm/problem/{id}``。用它当键而不是题名——
    #: 题名可能改，也可能带全角空格之类的怪字符。
    problem_id: str
    title: str
    verdict: str
    submitted_at: str


def _nowcoder_rows(page: str) -> list[NowcoderRow]:
    """从练习页的表格里取出每一行的题目 id、题名、判题结果和提交时间。

    题目那一格是 ``<a href="/acm/problem/318505">大户爱的期望</a>``。早先的
    做法先把 HTML 标签剥掉再取文字，链接里的 id 就丢了，只能拿题名当键。
    """
    rows: list[NowcoderRow] = []
    for raw in _NOWCODER_ROW_RE.findall(page):
        cells = _NOWCODER_CELL_RE.findall(raw)
        if len(cells) < 9:
            continue

        def text(html_cell: str) -> str:
            return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", html_cell))).strip()

        run_id = text(cells[0])
        submitted_at = text(cells[8])
        if not run_id.isdigit() or not _NOWCODER_TIME_RE.match(submitted_at):
            continue

        # 题目 id 在链接里，剥标签会丢掉，所以从原始 HTML 里取。
        link = re.search(r"/acm/problem/(\d+)", cells[1])
        if not link:
            continue

        rows.append(
            NowcoderRow(
                run_id=run_id,
                problem_id=link.group(1),
                title=text(cells[1]),
                verdict=text(cells[2]),
                submitted_at=submitted_at,
            )
        )
    return rows


def _parse_nowcoder_time(value: str) -> int | None:
    """把页面上的时间转成 epoch 秒。

    页面给的是北京时间（无时区标记），所以按东八区解析，不能当 UTC。
    """
    match = _NOWCODER_TIME_RE.match(value)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    try:
        return int(datetime(year, month, day, hour, minute, second, tzinfo=CST).timestamp())
    except ValueError:
        return None


_SOLVED_FETCHERS = {
    Platform.CODEFORCES: fetch_solved_codeforces,
    Platform.ATCODER: fetch_solved_atcoder,
    Platform.LUOGU: fetch_solved_luogu,
    Platform.NOWCODER: fetch_solved_nowcoder,
}


def fetch_solved(platform: str, identifier: str) -> SolvedSet:
    """读一个账号做过的题目。"""
    fetcher = _SOLVED_FETCHERS.get(platform)
    if fetcher is None:
        label = Platform.LABELS.get(platform, platform)
        raise FetchError(f"{label} 不提供解题记录，无法统计")

    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    try:
        with httpx.Client(
            headers=headers, timeout=DEFAULT_TIMEOUT, follow_redirects=False
        ) as client:
            return fetcher(client, identifier)
    except httpx.TimeoutException as exc:
        raise FetchError("请求超时，稍后重试") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"网络错误：{exc}") from exc
