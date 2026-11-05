"""Enrollment eligibility and review suggestions, never automatic award binding."""

import json
import unicodedata
from calendar import monthrange
from datetime import date, datetime

from .daytime import CST
from .honor_models import HonorEvent
from .models import User


def former_names(user: User) -> list[str]:
    return json.loads(user.former_names_json)


def study_start(user: User) -> date | None:
    if user.honor_study_start:
        return user.honor_study_start
    if 1900 <= user.enroll_year <= 9995:
        return date(user.enroll_year, 9, 1)
    return None


def study_end(user: User) -> date | None:
    if user.honor_study_end:
        return user.honor_study_end
    start = study_start(user)
    if not start:
        return None
    return start.replace(
        year=start.year + 4, day=min(start.day, monthrange(start.year + 4, start.month)[1])
    )


def event_is_complete(event: HonorEvent) -> bool:
    if event.occurred_on is None:
        return True
    last_day = event.occurred_on
    if event.date_precision == "month":
        last_day = last_day.replace(day=monthrange(last_day.year, last_day.month)[1])
    elif event.date_precision == "year":
        last_day = last_day.replace(month=12, day=31)
    return last_day <= datetime.now(CST).date()


def eligibility(user: User | None, event: HonorEvent) -> str:
    if user is None:
        return "待关联用户"
    start = study_start(user)
    if not start:
        return "待核实在校时间"
    if not event.occurred_on:
        return "待核实比赛日期"
    first = last = event.occurred_on
    if event.date_precision == "month":
        first = first.replace(day=1)
        last = first.replace(day=monthrange(first.year, first.month)[1])
    elif event.date_precision == "year":
        first = first.replace(month=1, day=1)
        last = first.replace(month=12, day=31)
    end = study_end(user)
    if last < start or (end and first >= end):
        return "不在在校时间内"
    if first < start or (end and last >= end):
        return "日期精度不足，需核对边界"
    if last > datetime.now(CST).date():
        return "比赛尚未结束"
    return "计入"


def user_view(user: User) -> dict:
    return {
        "id": user.id,
        "display_name": user.display_name,
        "student_id": user.student_id,
        "enroll_year": user.enroll_year,
        "former_names": former_names(user),
        "study_start": user.honor_study_start,
        "study_end": user.honor_study_end,
        "effective_study_start": study_start(user),
        "effective_study_end": study_end(user),
        "revision": user.honor_revision,
    }


def name_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def name_index(users: list[User]) -> dict[str, dict[int, str]]:
    index: dict[str, dict[int, str]] = {}
    for user in users:
        for alias in former_names(user):
            index.setdefault(name_key(alias), {})[user.id] = "former_name"
        index.setdefault(name_key(user.display_name), {})[user.id] = "current_name"
    return index


def match_candidates(raw_name, event, users, index):
    candidates = [
        {"user_id": uid, "matched_by": reason, "eligibility": eligibility(users[uid], event)}
        for uid, reason in index.get(name_key(raw_name), {}).items()
    ]
    eligible = [c["user_id"] for c in candidates if c["eligibility"] == "计入"]
    return candidates, eligible[0] if len(eligible) == 1 else None
