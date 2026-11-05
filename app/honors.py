"""Eligibility is evaluated on every read, including after enrollment corrections."""

from calendar import monthrange
from datetime import date, datetime

from .daytime import CST
from .honor_models import HonorEvent, HonorPerson


def study_end(person: HonorPerson) -> date | None:
    if person.study_end:
        return person.study_end
    if not person.study_start:
        return None
    start = person.study_start
    return start.replace(
        year=start.year + 4, day=min(start.day, monthrange(start.year + 4, start.month)[1])
    )


def eligibility(person: HonorPerson | None, event: HonorEvent) -> str:
    if person is None or person.merged_into_id is not None:
        return "待关联身份"
    if not person.study_start:
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
    end = study_end(person)
    if last < person.study_start or (end and first >= end):
        return "不在在校时间内"
    if first < person.study_start or (end and last >= end):
        return "日期精度不足，需核对边界"
    if last > datetime.now(CST).date():
        return "比赛尚未结束"
    return "计入"
