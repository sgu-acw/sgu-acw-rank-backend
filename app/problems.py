"""题目元信息：名称、难度、标签、链接。

题单要显示这些，而各平台的拿法差别很大：

* **Codeforces** 一次请求就能拿到全部一万多题的名称、rating 和标签。
  所以整批拉下来缓存，之后不再问。
* **洛谷** 逐题问，题面页的内嵌 JSON 里有 ``name``、``difficulty``、``tags``。
  难度和标签是数字 ID，需要码表翻译。
* **牛客** 没有题目查询接口，但提交记录里带着题名。加题时如果该成员做过
  这题，就能从记录里捡到名字，否则只有题号。
* **AtCoder** 没有公开的题目接口，只能从题面页 HTML 里抠标题。

缓存的理由是省事也省对方服务器的流量：题目名字和难度基本不变，抓一次就够。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import httpx

from .models import Platform

#: 洛谷难度是 1~7 的数字，官方没有给文字名，这是社区通行叫法。
LUOGU_DIFFICULTY = {
    0: "暂无评定",
    1: "入门",
    2: "普及−",
    3: "普及/提高−",
    4: "普及+/提高",
    5: "提高+/省选−",
    6: "省选/NOI−",
    7: "NOI/NOI+/CTSC",
}

#: 洛谷标签也是数字 ID。这里只收常见的那批，认不出的就原样丢掉——
#: 显示一个数字比不显示更让人困惑。
LUOGU_TAGS = {
    1: "模拟",
    2: "字符串",
    3: "动态规划",
    4: "图论",
    5: "贪心",
    6: "数学",
    7: "数据结构",
    8: "搜索",
    9: "二分",
    10: "分治",
    11: "排序",
    12: "递推",
    13: "枚举",
    14: "递归",
    15: "最短路",
    16: "最小生成树",
    17: "拓扑排序",
    18: "树形 DP",
    19: "状压 DP",
    20: "数论",
    21: "组合数学",
    22: "概率期望",
    23: "计算几何",
    24: "位运算",
    25: "并查集",
    26: "栈",
    27: "队列",
    28: "堆",
    29: "线段树",
    30: "树状数组",
    31: "哈希",
    32: "前缀和",
    33: "差分",
    34: "双指针",
    35: "离散化",
}


@dataclass
class ProblemInfo:
    """一道题的元信息。抓不到的部分留空，不要编。"""

    platform: str
    key: str
    name: str = ""
    rating: int | None = None
    tags: list[str] = field(default_factory=list)
    url: str = ""
    #: 有多少人过了。只有 CF 给，其他平台留空。
    solved_count: int | None = None
    #: 抓不到时记下原因，前端显示成「信息缺失」而不是空着让人以为是 bug。
    missing_reason: str = ""


def normalize_key(platform: str, key: str) -> str:
    """把用户填的题号规范成内部统一格式。

    目前只有牛客需要转换。牛客有两条来源，内部 key 带来源前缀
    （``nowcoder:contest:233234``、``nowcoder:main:{uuid}``），但用户不可能
    知道这个约定——他们手上只有一串数字。

    数字在牛客上是**竞赛站的题目 id**（``ac.nowcoder.com/acm/problem/233234``），
    主站的题是 uuid，不是数字。实测纯数字走竞赛站能取到标题，走主站会返回
    「试题不存在」。所以纯数字一律按竞赛站处理。

    这个函数必须在**写入题单之前**调用。否则题单里存的是裸数字，而过题记录
    里存的是带前缀的 key，两边对不上——完成度永远算成 0，题名也抓不到。
    生产库里已经有 45 条这样的历史数据，靠迁移脚本修的。
    """
    value = (key or "").strip()
    if not value:
        return value

    if platform == Platform.NOWCODER:
        # 已经带前缀的原样返回。
        if value.startswith("nowcoder:"):
            return value
        # 纯数字 → 竞赛站。主站的题是 uuid，用户不会只填数字。
        if value.isdigit():
            return f"nowcoder:contest:{value}"
        # 带字母的当作主站的 uuid。
        return f"nowcoder:main:{value}"

    # 洛谷和 Codeforces 的题号规范是大写：P1001、B4249、1304B、1838A。
    # 管理员随手输小写也能查到（API 不挑），但过题记录里存的是规范大写，
    # 按原值匹配就永远算「没过」。统一大写，让题单/集训两边对得上。
    if platform in (Platform.LUOGU, Platform.CODEFORCES):
        return value.upper()

    # AtCoder 反过来，题号是小写：abc123_a、dp_h、agc001_f。大写会查不到。
    if platform == Platform.ATCODER:
        return value.lower()

    # 本地练习题的 key 就是题目 id；去掉前导零保持唯一约束语义一致。
    if platform == Platform.LEARN:
        return str(int(value)) if value.isdigit() else ""

    return value


def parse_nowcoder_key(key: str) -> tuple[str, str]:
    """把牛客题号拆成 ``(来源, id)``。来源是 ``contest`` 或 ``main``。

    牛客的题号有三种写法，都指向同一批题：

    * ``nowcoder:contest:233234`` —— 内部标准格式。前缀是为了跟
      ``nowcoder:main:{uuid}`` 区分开：牛客有两条来源，id 体系不同。
    * ``contest:233234`` —— 去掉了平台名的写法。
    * ``233234`` —— 迁移之前存进库的裸数字。

    数字是竞赛站的题目 id（主站的题是 uuid，不是数字），所以裸数字归竞赛站。

    单独抽出来是因为拼链接和抓题目信息都要用。两处各写一份的话很容易走偏——
    实际上就走过：拼链接那边按 ``split(":", 1)`` 解析，遇到带平台前缀的标准
    格式会把 ``nowcoder`` 当成来源，两个分支都不匹配，全部落到搜索页兜底。
    """
    raw = key[len("nowcoder:") :] if key.startswith("nowcoder:") else key

    if raw.isdigit():
        return "contest", raw

    source, _, problem_id = raw.partition(":")
    if not problem_id:
        # 认不出的格式（比如早期的 ``nowcoder:题名``），当主站题名处理。
        return "legacy", raw
    return source, problem_id


def problem_url(platform: str, key: str) -> str:
    """按题号拼题面链接。

    各平台的 URL 规则：
    * CF  ``1234A`` → ``/contest/1234/problem/A``。但 gym 的题（题号里带
      字母前缀如 ``102769E``）走的是 gym 路径，两者规则不同。
    * 洛谷 ``P1001`` → ``/problem/P1001``
    * AtCoder ``abc300_a`` → ``/contests/abc300/tasks/abc300_a``
    * 牛客没有稳定的题面 URL，交给搜索页。
    """
    if platform == Platform.CODEFORCES:
        match = re.fullmatch(r"(\d+)([A-Z]\d?)", key)
        if match:
            contest, index = match.groups()
            # 正式比赛是 4 位以内的编号；超过 100000 的基本都是 gym。
            if int(contest) >= 100000:
                return f"https://codeforces.com/gym/{contest}/problem/{index}"
            return f"https://codeforces.com/contest/{contest}/problem/{index}"
        return "https://codeforces.com/problemset?order=BY_SOLVED_DESC&locale=en"

    if platform == Platform.LUOGU:
        return f"https://www.luogu.com.cn/problem/{key}"

    if platform == Platform.ATCODER:
        # 按最后一个下划线切：前面是比赛名，后面是题号。
        #
        # 不能用「字母 + 数字」这种模式去猜比赛名——AtCoder 的比赛名
        # 千奇百怪：``dp``（没数字）、``typical90``（数字在末尾）、
        # ``abc001``、``joi2020yo1a`` 都有。原来那个 ``[a-z]+\d+`` 要求
        # 字母后面紧跟数字，``dp_h`` 这种就匹配不上，落到兜底地址，
        # 于是抓回来的是竞赛列表页，取不到标题。
        contest, _, suffix = key.rpartition("_")
        if contest and suffix:
            return f"https://atcoder.jp/contests/{contest}/tasks/{key}"
        return "https://atcoder.jp/contests/"

    if platform == Platform.LEARN:
        return f"/learn/problem?id={key}"

    if platform == Platform.NOWCODER:
        source, problem_id = parse_nowcoder_key(key)
        if source == "contest":
            return f"https://ac.nowcoder.com/acm/problem/{problem_id}"
        if source == "main":
            return f"https://www.nowcoder.com/questionTerminal/{problem_id}"
        return "https://www.nowcoder.com/exam/oj"

    return ""


# --------------------------------------------------------------------------- #
# Codeforces：一次拉全量
# --------------------------------------------------------------------------- #


def fetch_all_codeforces(client: httpx.Client) -> list[ProblemInfo]:
    """拉取 CF 全部题目。

    一个请求拿回一万多题的名称、rating 和标签。这是唯一一个能整批拿的平台，
    所以直接全量缓存，之后不再请求。
    """
    response = client.get("https://codeforces.com/api/problemset.problems")
    if response.status_code != 200:
        raise RuntimeError(f"Codeforces 返回 {response.status_code}")
    payload = response.json()
    if payload.get("status") != "OK":
        raise RuntimeError(f"Codeforces: {payload.get('comment', '查询失败')}")

    # 过题人数在另一个数组里，按 (比赛 id, 题号) 对起来。
    solved_by_key: dict[tuple[int, str], int] = {}
    for stat in payload["result"].get("problemStatistics", []):
        contest_id = stat.get("contestId")
        index = stat.get("index")
        if contest_id is None or index is None:
            continue
        solved_by_key[(contest_id, index)] = int(stat.get("solvedCount") or 0)

    out: list[ProblemInfo] = []
    for problem in payload["result"]["problems"]:
        contest_id = problem.get("contestId")
        index = problem.get("index")
        if contest_id is None or index is None:
            continue
        key = f"{contest_id}{index}"
        out.append(
            ProblemInfo(
                platform=Platform.CODEFORCES,
                key=key,
                name=problem.get("name", ""),
                rating=problem.get("rating"),
                tags=list(problem.get("tags", [])),
                url=problem_url(Platform.CODEFORCES, key),
                solved_count=solved_by_key.get((contest_id, index)),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 洛谷：逐题
# --------------------------------------------------------------------------- #


def fetch_luogu_problem(client: httpx.Client, key: str) -> ProblemInfo:
    """抓洛谷单题。复用绑定账号时那套反爬握手。"""
    from .platforms import _luogu_context, _luogu_get

    info = ProblemInfo(platform=Platform.LUOGU, key=key, url=problem_url(Platform.LUOGU, key))
    try:
        page = _luogu_get(client, info.url)
    except Exception as exc:
        info.missing_reason = f"抓取失败：{exc}"
        return info

    problem = ((_luogu_context(page).get("data") or {}).get("problem")) or {}
    if not problem:
        info.missing_reason = "洛谷上没有这道题"
        return info

    info.name = problem.get("name") or ""
    difficulty = problem.get("difficulty")
    info.rating = int(difficulty) if isinstance(difficulty, int) and difficulty > 0 else None
    info.tags = [
        LUOGU_TAGS[tag]
        for tag in (problem.get("tags") or [])
        if isinstance(tag, int) and tag in LUOGU_TAGS
    ]
    return info


# --------------------------------------------------------------------------- #
# AtCoder：从题面页抠标题
# --------------------------------------------------------------------------- #


def fetch_atcoder_problem(client: httpx.Client, key: str) -> ProblemInfo:
    """AtCoder 没有题目接口，只能从题面页的 HTML 里取标题。

    难度和标签拿不到——AtCoder 的难度是社区算出来的，不在官方页面上。
    """
    info = ProblemInfo(platform=Platform.ATCODER, key=key, url=problem_url(Platform.ATCODER, key))
    try:
        response = client.get(info.url)
    except Exception as exc:
        info.missing_reason = f"抓取失败：{exc}"
        return info

    if response.status_code == 404:
        info.missing_reason = "AtCoder 上没有这道题"
        return info
    if response.status_code != 200:
        info.missing_reason = f"AtCoder 返回 {response.status_code}"
        return info

    # 标题在 <span class="h2">A - Something</span> 里，但同一个 span 后面
    # 还跟着「Editorial」链接，抠出来会带上它，所以要截到第一个标签为止。
    match = re.search(r'<span class="h2">([^<]*)', response.text)
    if match:
        title = html.unescape(match.group(1)).strip()
        # 标题形如「A - Two Choices」或「001 - Yokan Party」，去掉题号。
        # 题号可能是字母（A、Ex）或数字（001），后面跟着分隔符。
        info.name = re.sub(r"^[A-Za-z0-9]{1,3}\s*[-–—]\s*", "", title).strip()
    else:
        info.missing_reason = "页面结构变了，取不到标题"
    return info


# --------------------------------------------------------------------------- #
# 牛客：从提交记录里捡题名
# --------------------------------------------------------------------------- #


def fetch_nowcoder_problem(client: httpx.Client, key: str) -> ProblemInfo:
    """牛客的题目信息。

    题号格式是 ``nowcoder:{来源}:{题目 id}``，来源是 ``main`` 或 ``contest``。
    两边的 id 体系不同，题面路径也不同：

    * 竞赛站 ``contest``：``/acm/problem/{id``，标题就是题目名。
    * 主站 ``main``：``/questionTerminal/{uuid}``，但过题记录里存的是数字
      ``questionId``，拼不出这个路径，所以抓不到名字。

    抓不到名字不算错——题单里显示题号也是可用的，只是不够好看。
    """
    source, problem_id = parse_nowcoder_key(key)

    if source == "legacy":
        # 早期的 ``nowcoder:题名`` 格式，题名就是全部信息。
        return ProblemInfo(
            platform=Platform.NOWCODER,
            key=key,
            name=problem_id,
            url=problem_url(Platform.NOWCODER, problem_id),
        )

    if source == "contest":
        url = f"https://ac.nowcoder.com/acm/problem/{problem_id}"
        info = ProblemInfo(platform=Platform.NOWCODER, key=key, url=url)
        try:
            response = client.get(url)
        except Exception as exc:  # noqa: BLE001
            info.missing_reason = f"抓取失败：{exc}"
            return info

        if response.status_code != 200:
            info.missing_reason = f"牛客返回 {response.status_code}"
            return info

        # 标题形如「小月的对局_牛客竞赛OJ」，去掉后缀。
        match = re.search(r"<title>(.*?)</title>", response.text, re.S)
        if match:
            title = html.unescape(match.group(1)).strip()
            info.name = re.sub(r"_(牛客|牛客竞赛OJ|牛客网).*$", "", title).strip()
        if not info.name:
            info.missing_reason = "取不到题目名"
        return info

    # 主站：key 里存的是 questionUuid，能拼出题面地址。
    url = f"https://www.nowcoder.com/questionTerminal/{problem_id}"
    info = ProblemInfo(platform=Platform.NOWCODER, key=key, url=url)
    try:
        response = client.get(url)
    except Exception as exc:  # noqa: BLE001
        info.missing_reason = f"抓取失败：{exc}"
        return info

    if response.status_code != 200:
        info.missing_reason = f"牛客返回 {response.status_code}"
        return info

    # 标题形如「多组输入a+b II__牛客网」，去掉后缀。
    match = re.search(r"<title>(.*?)</title>", response.text, re.S)
    if match:
        title = html.unescape(match.group(1)).strip()
        info.name = re.sub(r"_{1,2}牛客网?.*$", "", title).strip()
    if not info.name:
        info.missing_reason = "取不到题目名"
    return info


FETCHERS = {
    Platform.CODEFORCES: fetch_all_codeforces,
    Platform.LUOGU: fetch_luogu_problem,
    Platform.ATCODER: fetch_atcoder_problem,
    Platform.NOWCODER: fetch_nowcoder_problem,
}


def fetch_one(client: httpx.Client, platform: str, key: str) -> ProblemInfo:
    """抓单题。CF 走全量接口但这里只返回要的那道。"""
    if platform == Platform.CODEFORCES:
        # 单题无法单独查，但缓存里应该已经有了。没有就返回空壳，
        # 由调用方决定要不要整批刷新。
        # 措辞要给出下一步动作。管理员看到「不存在」会以为是自己的错，
        # 但更常见的情况是题库还没同步过，或者题号真的写错了。
        return ProblemInfo(
            platform=platform,
            key=key,
            url=problem_url(platform, key),
            missing_reason="题库里没有这个题号。先点「刷新 CF 题库」，再确认题号是否写对。",
        )

    fetcher = FETCHERS.get(platform)
    if fetcher is None:
        return ProblemInfo(platform=platform, key=key, missing_reason=f"不支持的平台 {platform}")
    return fetcher(client, key)
