"""Parsing for Shaoguan University student ids.

The encoding is documented in the sgu-aio repository under
``docs/学号规则/README.md``; this module is the server-side counterpart.

Eleven digits::

    [入学年份 2][培养层次 1][学院码 2][专业号 2][班级 1][序号 3]

Joint-programme students get a twelfth character, an uppercase letter, and
their college code sits in the 94-97 range.

Two caveats carried over from that document, both of which matter here:

* The first two digits record the year of *enrolment*, not the current year of
  study. A student who transfers, resumes or repeats a year keeps the original
  number, so about 7% of ids disagree with their class year.
* The id is not a secret. It shows up in scholarship and award listings, so it
  works as a login name but never as proof of identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: College codes seen in the public listings. Kept as a set because a new
#: college needs a deliberate edit here rather than silently passing through.
COLLEGE_CODES: frozenset[str] = frozenset(
    {
        "01",  # 文学与传媒学院
        "02",  # 政法学院 / 马克思主义学院（共用）
        "03",  # 政法学院
        "04",  # 外国语学院
        "05",  # 数学与统计学院
        "06",  # 化学与土木工程学院
        "08",  # 生物与农业学院
        "09",  # 生物与农业学院
        "10",  # 食品学院
        "15",  # 美术与设计学院
        "16",  # 音乐与舞蹈学院
        "17",  # 商学院
        "18",  # 旅游与地理学院
        "19",  # 体育学院
        "20",  # 教育科学学院（韶州师范学院）
        "22",  # 化学与土木工程学院
        "24",  # 智能工程学院
        "25",  # 信息工程学院
        "49",  # 医学院（21 级临床医学特殊码）
        "51",  # 医学院
        "94",  # 联培
        "95",  # 联培
        "96",  # 联培
        "97",  # 联培
    }
)

#: 1 普通本科, 2 专科, 3 专升本 / 协同育人.
LEVELS: dict[int, str] = {1: "普通本科", 2: "专科", 3: "专升本/协同育人"}

#: College codes reserved for joint programmes.
LINKED_COLLEGE_CODES: frozenset[str] = frozenset({"94", "95", "96", "97"})

_ID_RE = re.compile(r"^(\d{2})(\d)(\d{2})(\d{2})(\d)(\d{3})([A-Z])?$")

#: 校外访客用户名规则：以英文字母开头，3-16 位，仅含小写字母、数字、下划线和短横线。
#: 所有校内学号与 root 占位号均以数字开头，两者天然互斥、永不冲突。
_EXTERNAL_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,15}$")


#: root 账号的学号。十一个 0 不是合法学号——学院码 00 不在集合里，所以别人
#: 注册不到这个号。root 不走学号解析，创建时直接填占位值。
ROOT_STUDENT_ID = "00000000000"


@dataclass(frozen=True, slots=True)
class StudentId:
    """A parsed student id.

    ``raw`` keeps the normalised input so the database stores exactly what the
    student typed (upper-cased, trimmed) rather than a reconstruction.
    """

    raw: str
    enroll_year: int
    level: int
    college_code: str
    major_code: str
    class_no: int
    serial: int
    link_letter: str | None = None

    @property
    def is_linked(self) -> bool:
        """True for the twelve-character joint-programme form."""
        return self.link_letter is not None

    @property
    def level_name(self) -> str:
        return LEVELS.get(self.level, f"未知({self.level})")


class StudentIdError(ValueError):
    """Raised when a string cannot be read as a student id."""


def parse_student_id(value: str) -> StudentId:
    """Parse ``value`` into a :class:`StudentId` or raise :class:`StudentIdError`.

    The error message is deliberately generic: it is surfaced to the client, and
    a precise reason would help someone guess valid ids.
    """
    text = (value or "").strip().upper()
    match = _ID_RE.match(text)
    if not match:
        raise StudentIdError("学号格式不正确")

    year, level, college, major, class_no, serial, letter = match.groups()
    if college not in COLLEGE_CODES:
        raise StudentIdError("学号格式不正确")

    level_int = int(level)
    if level_int not in LEVELS:
        raise StudentIdError("学号格式不正确")

    # A twelfth character only exists on joint-programme ids, which are exactly
    # the ones carrying a linked college code.
    if (letter is not None) != (college in LINKED_COLLEGE_CODES):
        raise StudentIdError("学号格式不正确")

    return StudentId(
        raw=text,
        enroll_year=2000 + int(year),
        level=level_int,
        college_code=college,
        major_code=major,
        class_no=int(class_no),
        serial=int(serial),
        link_letter=letter,
    )


def is_root_student_id(value: str) -> bool:
    """是否是 root 的占位学号。"""
    return (value or "").strip() == ROOT_STUDENT_ID


def is_valid_student_id(value: str) -> bool:
    try:
        parse_student_id(value)
    except StudentIdError:
        return False
    return True


def parse_external_username(value: str) -> str:
    """校验并归一化校外人员用户名（全小写）。"""
    text = (value or "").strip().lower()
    if not _EXTERNAL_USERNAME_RE.match(text):
        raise StudentIdError("用户名须为 3–16 位，以英文字母开头，仅限字母、数字、下划线和短横线")
    return text


def is_valid_external_username(value: str) -> bool:
    try:
        parse_external_username(value)
    except StudentIdError:
        return False
    return True


def normalize_login_identifier(value: str) -> str:
    """登录账号名归一化：以字母开头视作校外用户名（小写），否则按学号处理（大写）。"""
    text = (value or "").strip()
    if not text:
        return ""
    if text[0].isascii() and text[0].isalpha():
        return text.lower()
    return text.upper()
