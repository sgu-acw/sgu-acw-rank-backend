"""Request and response schemas.

Kept separate from the table definitions so that a database column can never
leak into a response by accident. Anything a client may see is listed here
explicitly.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import Platform, ProblemListKind, Role


class UserPublic(BaseModel):
    """What any logged-in member may see about another member."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    student_id: str
    display_name: str
    enroll_year: int
    level: int
    college_code: str
    major_code: str
    #: 班级序号。单独的「3 班」没有意义，所以 class_label 是专业加序号。
    class_no: int = 0
    #: 学院名与专业名。管理员可改，所以可能跟自己的学号码对不上。
    college_name: str | None = None
    major_name: str | None = None
    class_label: str | None = None
    role: str
    status: str
    created_at: datetime


class UserSelf(UserPublic):
    """What a member sees about themselves. Adds nothing private for now, but
    keeps a distinct type so fields can diverge without touching callers."""

    #: 两步验证是否已开——/me 页设置项的显示状态，登录流程判断是否弹码。
    totp_enabled: bool = False


class AdminAccountBrief(BaseModel):
    """成员列表里显示的平台账号摘要。

    列表页只需要「绑了什么、验没验过」，用不着详情页那二十几个字段
    （评分、抓取时间、失败次数……）。单独定义一个类型，避免列表接口
    为了塞满 ``AdminAccountDetail`` 而多查一堆东西。
    """

    id: int
    platform: str
    identifier: str
    #: 平台上的昵称，抓不到时为 None。
    display_name: str | None = None
    #: 验证通过的时间。为 None 表示还没验证，不计入榜单。
    verified_at: datetime | None = None


class UserAdmin(UserPublic):
    """Admin view: adds the admin-only note."""

    admin_note: str
    last_login_at: datetime | None = None
    #: 能不能发文章（博客/公告）。管理员天然能，这里表示成员被勾了没有。
    can_post: bool = False

    #: 这个成员绑定的平台账号。列表页直接显示，不用再点进详情。
    accounts: list[AdminAccountBrief] = Field(default_factory=list)


class RegisterRequest(BaseModel):
    invite_code: str = Field(min_length=1, max_length=32)
    student_id: str = Field(min_length=1, max_length=16)
    display_name: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("姓名不能为空")
        return cleaned


class LoginRequest(BaseModel):
    student_id: str = Field(min_length=1, max_length=16)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class UpdateSelfRequest(BaseModel):
    """Fields a member may change about themselves.

    Student id, role and status are deliberately absent: the first is the login
    identity, the other two are administrative.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=32)

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("姓名不能为空")
        return cleaned


class InviteCreate(BaseModel):
    max_uses: int = Field(default=1, ge=1, le=500)
    ttl_hours: int = Field(default=24, ge=1, le=24 * 365)
    grants_role: str = Field(default=Role.MEMBER)
    note: str = Field(default="", max_length=500)

    @field_validator("grants_role")
    @classmethod
    def _known_role(cls, value: str) -> str:
        if value not in Role.ALL:
            raise ValueError("未知的角色")
        return value


class InvitePublic(BaseModel):
    """Invite as shown to administrators."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    grants_role: str
    max_uses: int
    use_count: int
    expires_at: datetime
    revoked_at: datetime | None
    note: str
    #: 创建者的 id。前端一般不用它，看下面两个字段就够了。
    created_by: int | None
    #: 创建者姓名与学号。
    #:
    #: 邀请码是要追责的——码被转发出去、或者发错了人，得知道找谁问。
    #: 只给一个数字 id 等于没给。姓名可能重名，所以带上学号。
    created_by_name: str | None = None
    created_by_student_id: str | None = None
    created_at: datetime
    is_usable: bool


class UpdateUserAdminRequest(BaseModel):
    """Administrative edits to another member.

    ``admin_note`` is the admin-only field; an empty string clears it. Sending
    ``None`` leaves it untouched.

    学号不在可改字段里：它是登录名，改它等于换了个账号。真需要纠正时走
    单独的接口，那里会一并检查是否与现有学号冲突。
    """

    role: str | None = None
    status: str | None = None
    admin_note: str | None = Field(default=None, max_length=2000)
    display_name: str | None = Field(default=None, min_length=1, max_length=32)
    #: 学院名/专业名的人工覆盖。传空串表示恢复成按学号查表。
    #:
    #: 名字带 override 是为了跟响应里的 college_name 区分——后者是解析结果
    #: （可能是查表得来的），直接把响应的值原样 PATCH 回来不会意外把它钉成
    #: 人工覆盖。
    college_override: str | None = Field(default=None, max_length=64)
    #: 「可发文章」开关。给成员开后他能写文章（弹窗和上下架还是管理员管）。
    can_post: bool | None = None
    major_override: str | None = Field(default=None, max_length=64)
    #: 班级序号，学号第 8 位。0 表示未分班（不显示班级）。
    class_no: int | None = Field(default=None, ge=0, le=99)

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: str | None) -> str | None:
        if value is not None and value not in Role.ALL:
            raise ValueError("未知的角色")
        return value

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str | None) -> str | None:
        if value is not None and value not in ("active", "disabled"):
            raise ValueError("未知的状态")
        return value

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("姓名不能为空")
        return cleaned


class AdminCreateUserRequest(BaseModel):
    """管理员直接建号，不需要邀请码。

    用于代那些不自己注册的人建号，比如已经毕业但还在榜上的成员。
    """

    student_id: str = Field(min_length=1, max_length=16)
    display_name: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(default=Role.MEMBER)
    admin_note: str = Field(default="", max_length=2000)

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("姓名不能为空")
        return cleaned

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: str) -> str:
        if value not in Role.ALL:
            raise ValueError("未知的角色")
        return value


class AdminUserPage(BaseModel):
    """成员列表的一页。带上总数与筛选后的数量，供分页控件使用。"""

    items: list[UserAdmin]
    total: int
    page: int
    page_size: int
    #: 可选值，让前端不必写死年级下拉的选项。
    enroll_years: list[int]
    college_codes: list[str]


class AccountPublic(BaseModel):
    """一个平台账号。未验证的也会返回，好让成员看到待办状态。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: str
    identifier: str
    verified_at: datetime | None
    #: 仅在未验证时出现，用于告诉成员该把什么填进签名。
    verify_token: str | None = None
    verify_expires_at: datetime | None = None
    #: 账号状态标记：privacy = 开了平台隐私保护，unverified = 未实名。
    #: ``None`` 表示正常。成员自己也要看到这个状态，才知道该去关隐私设置。
    sync_flag: str | None = None
    created_at: datetime


class AccountCreate(BaseModel):
    platform: str = Field(min_length=1, max_length=16)
    #: Codeforces/AtCoder 的 handle，或洛谷/牛客的 uid。洛谷也接受用户名。
    identifier: str = Field(min_length=1, max_length=64)


class AccountVerifyResult(BaseModel):
    ok: bool
    detail: str
    account: AccountPublic | None = None


class RankEntry(BaseModel):
    """榜单的一行。一个人可能有多个账号，所以以账号为单位而非以人为单位。"""

    user_id: int
    display_name: str
    student_id: str
    enroll_year: int
    platform: str
    identifier: str
    rank: int = 0

    rating: int | None = None
    max_rating: int | None = None
    #: 平台上的显示名，可能与 identifier 不同。
    platform_name: str | None = None
    #: 抓取失败的原因。有值时其余数据为空，界面照常显示这一行但标注异常。
    error: str | None = None


class RankResponse(BaseModel):
    platform: str
    entries: list[RankEntry]
    fetched_at: int
    cache_ttl: int


class PracticeDay(BaseModel):
    date: str
    count: int


class PracticeFilterOptions(BaseModel):
    """榜单可用的筛选项。

    从**全部**有数据的人里统计，而不是从当前筛选结果里——否则选了一个学院
    之后，其他学院就从下拉里消失，想换一个还得先清掉。
    """

    colleges: list[str] = Field(default_factory=list)
    enroll_years: list[int] = Field(default_factory=list)


class DailyPracticeEntry(BaseModel):
    """每日练习榜的一行，以人为单位（同平台多个账号已合并）。"""

    user_id: int
    display_name: str
    student_id: str
    enroll_year: int
    rank: int = 0

    #: 学院与专业。学院名可能被管理员改过，所以不一定跟 college_code 对得上。
    college_code: str = ""
    major_code: str = ""
    college_name: str | None = None
    major_name: str | None = None
    class_label: str | None = None

    #: 统计窗口内的过题总数。
    total: int = 0
    #: 窗口内每一天的数量，顺序与 dates 一致，缺失的补 0。
    days: list[PracticeDay] = Field(default_factory=list)


class DailyPracticeResponse(BaseModel):
    days: int
    platform: str | None
    #: 窗口内的日期，从早到晚。
    dates: list[str]
    entries: list[DailyPracticeEntry]
    generated_at: int
    #: 可供筛选的学院与年级。
    filters: PracticeFilterOptions = Field(default_factory=PracticeFilterOptions)


class SyncStatus(BaseModel):
    has_run: bool
    started_at: datetime | None = None
    finished_at: datetime | None = None
    account_count: int = 0
    ok_count: int = 0
    error_count: int = 0
    #: 失败账号的说明，供管理员排查。
    errors: list[str] = Field(default_factory=list)


class RatingEntry(BaseModel):
    """rating 榜的一行。

    以**账号**为单位而不是以人为单位：一个人在同一平台可以有主号和小号，
    两个号各有各的 rating，合并成一个数没有意义。
    """

    user_id: int
    display_name: str
    student_id: str
    enroll_year: int
    rank: int = 0

    #: 学院、专业、班级。与练习量榜保持一致，免得两个维度显示的信息不一样。
    college_code: str = ""
    major_code: str = ""
    college_name: str | None = None
    major_name: str | None = None
    class_label: str | None = None

    platform: str
    identifier: str
    #: 平台上的昵称，与 identifier 不一定相同（牛客的 identifier 是数字 uid）。
    platform_name: str | None = None

    rating: int | None = None
    max_rating: int | None = None
    #: rating 是多久以前抓的，用于提示数据新旧。
    fetched_at: datetime | None = None


class RatingResponse(BaseModel):
    """某个平台的 rating 榜。

    没有「全部平台」这个选项：各平台的 rating 不在同一把尺子上，混在一起
    排序没有意义。
    """

    platform: str
    label: str
    entries: list[RatingEntry]
    generated_at: int


class DashboardAccount(BaseModel):
    """面板上的一个平台账号。"""

    platform: str
    label: str
    identifier: str
    platform_name: str | None = None
    verified: bool
    rating: int | None = None
    max_rating: int | None = None


class DashboardSummary(BaseModel):
    """个人数据面板。

    一次请求给齐，避免首页开五个口子各自加载、各自闪一下。
    """

    #: 今天是哪个练习日（凌晨 4 点为界）。
    practice_day: str

    #: 今日、最近 7 天、累计的过题数。
    today: int = 0
    week: int = 0
    total: int = 0

    #: 最近 30 个练习日的每日过题数，从早到晚。
    days: list[PracticeDay] = Field(default_factory=list)

    #: 在练习榜上的名次（最近 7 天），未上榜时为 None。
    practice_rank: int | None = None
    #: 练习榜总人数，用于显示「3 / 20」。
    practice_total: int = 0

    accounts: list[DashboardAccount] = Field(default_factory=list)

    #: 本周还剩几次手动刷新机会。
    refresh_remaining: int = 0
    refresh_limit: int = 0


class AcademicOptions(BaseModel):
    """学院与专业的候选列表，供管理员改学院/专业时选。

    数据来自学号规则反推出的码表。联培码（94-97）下会挂着不同学院的专业，
    所以同一个专业名可能出现在多个学院下——这是数据本身的样子，不是错。
    """

    colleges: list[str]
    #: 学院名 → 该学院下的专业名（去重排序）。
    majors: dict[str, list[str]]


# --------------------------------------------------------------------------- #
# 题单
# --------------------------------------------------------------------------- #


class ProblemEntry(BaseModel):
    """题单里的一道题，带上题目元信息和某个人的完成状态。"""

    id: int
    platform: str
    key: str
    position: int
    note: str = ""

    #: 以下来自题目缓存。抓不到时 name 为空，missing_reason 说明原因。
    name: str = ""
    rating: int | None = None
    tags: list[str] = Field(default_factory=list)
    url: str = ""
    missing_reason: str = ""

    #: 当前查看的人过了没有。未登录时恒为 None——进度是个人数据。
    done: bool | None = None


class ProblemListPublic(BaseModel):
    """题单或题单合集，看列表时用。"""

    id: int
    kind: str
    title: str
    description: str
    collection_id: int | None
    position: int
    created_at: datetime
    updated_at: datetime

    #: 题目数（题单）或子题单数（合集）。
    item_count: int = 0
    #: 已完成的题数。合集是它下面所有题单的合计。
    done_count: int = 0
    #: 完成度 = done_count / total_problems。合集是总题数。
    total_problems: int = 0


class ProblemListDetail(ProblemListPublic):
    """题单详情。题单带题目列表，合集带子题单。"""

    problems: list[ProblemEntry] = Field(default_factory=list)
    children: list[ProblemListPublic] = Field(default_factory=list)


class ProblemListCreate(BaseModel):
    kind: str = Field(default=ProblemListKind.LIST)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    collection_id: int | None = None
    admin_note: str = Field(default="", max_length=500)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in ProblemListKind.ALL:
            raise ValueError("题单类型只能是 list 或 collection")
        return value


class ProblemListUpdate(BaseModel):
    """改题单。None 表示不动这个字段。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    collection_id: int | None = None
    admin_note: str | None = Field(default=None, max_length=500)
    #: 换到别的合集。用 ``set_collection`` 区分「不改」和「移出合集」。
    set_collection: bool = False


class ProblemItemCreate(BaseModel):
    platform: str
    key: str = Field(min_length=1, max_length=128)
    note: str = Field(default="", max_length=500)

    @field_validator("platform")
    @classmethod
    def _known_platform(cls, value: str) -> str:
        from .learning_helpers import learning_enabled

        if value not in Platform.ALL and not (value == Platform.LEARN and learning_enabled()):
            raise ValueError("未知的平台")
        return value


class ProblemItemUpdate(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class ReorderRequest(BaseModel):
    """拖拽排序。给一串 id，按给出的顺序重排。"""

    ids: list[int] = Field(min_length=1)


class ProblemLookup(BaseModel):
    """按题号查题目信息，供管理员加题时预览。"""

    platform: str
    key: str
    name: str = ""
    rating: int | None = None
    tags: list[str] = Field(default_factory=list)
    url: str = ""
    missing_reason: str = ""
    #: 这个题号在现有缓存里有没有。没有的话前端提示「未收录」。
    cached: bool = False


# --------------------------------------------------------------------------- #
# 管理员视角的成员详情
# --------------------------------------------------------------------------- #


class AdminAccountDetail(BaseModel):
    """管理员看到的一个平台账号。

    比成员自己看到的多出同步健康度：抓取时间、失败次数、最近错误。这些
    是用来排查「这个人为什么没数据」的，对成员本人没有意义。
    """

    id: int
    platform: str
    identifier: str
    display_name: str | None = None

    verified_at: datetime | None = None
    #: 验证尝试次数。冷却时间随它翻倍。
    verify_attempts: int = 0
    verify_attempted_at: datetime | None = None

    rating: int | None = None
    max_rating: int | None = None
    rating_fetched_at: datetime | None = None
    solved_fetched_at: datetime | None = None
    #: 最后一次**抓到新题**的时间。与 solved_fetched_at 不同：后者只是
    #: 「最后一次去看」。
    last_active_at: datetime | None = None

    #: 最近一次抓取出错的原因。
    last_error: str | None = None
    consecutive_failures: int = 0
    rating_failures: int = 0
    #: 账号状态标记（privacy / unverified）。
    sync_flag: str | None = None

    created_at: datetime
    #: 已过题目数。来自最近一次快照。
    solved_count: int = 0
    #: 有多少天的练习记录。
    active_days: int = 0


class AdminMemberDetail(BaseModel):
    """管理员视角的成员详情。

    在公开信息之外，加上排查用的那一块：备注、最后登录、账号同步状态。
    """

    id: int
    display_name: str
    student_id: str
    enroll_year: int
    level: int
    college_code: str
    major_code: str
    college_name: str | None = None
    major_name: str | None = None
    class_label: str | None = None
    role: str
    status: str

    #: 仅管理员可见的备注。
    admin_note: str = ""
    last_login_at: datetime | None = None
    created_at: datetime

    accounts: list[AdminAccountDetail] = Field(default_factory=list)

    #: 汇总，给一眼看的数字。
    total_solved: int = 0
    #: 最近 30 天有几天做过题。
    active_days_30: int = 0
    #: 最后一次做对题是哪天。用来判断这个人是不是掉队了。
    last_practice_date: str | None = None
    #: 有几个账号处于连续失败状态。
    failing_accounts: int = 0


class ProblemListRankEntry(BaseModel):
    """题单完成度排行榜的一行。"""

    user_id: int
    display_name: str
    student_id: str
    enroll_year: int
    college_name: str | None = None
    major_name: str | None = None
    class_label: str | None = None

    done_count: int = 0
    total: int = 0
    #: 完成度百分比，0~100。四舍五入到整数。
    percent: int = 0
    #: 最后一次过这个题单里的题是哪天。
    last_done_date: str | None = None


class ProblemListRank(BaseModel):
    """题单完成度排行榜。"""

    list_id: int
    title: str
    total: int
    entries: list[ProblemListRankEntry] = Field(default_factory=list)
