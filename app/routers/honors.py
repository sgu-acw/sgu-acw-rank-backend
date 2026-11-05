"""Public honors and an admin-only review queue. Imports append without name matching."""

import json
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, model_validator
from sqlalchemy import update
from sqlmodel import Session, select

from ..db import get_session
from ..deps import get_current_user, require_admin, require_origin_secret
from ..honor_models import HonorAward, HonorEvent, HonorPerson, HonorRecipient
from ..honors import eligibility, study_end
from ..models import User

router = APIRouter(prefix="/honors", tags=["honors"], dependencies=[Depends(require_origin_secret)])
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Key = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersonInput(Input):
    name: Text
    aliases: list[Text] = Field(default_factory=list, max_length=30)
    user_id: int | None = Field(default=None, gt=0)
    study_start: date | None = None
    study_end: date | None = None
    note: str = Field(default="", max_length=3000)
    revision: int | None = None

    @model_validator(mode="after")
    def dates(self):
        if self.study_end and (not self.study_start or self.study_end <= self.study_start):
            raise ValueError("离校日期必须晚于入学日期")
        if self.study_start and self.study_start.year > 9995:
            raise ValueError("入学日期超出支持范围")
        return self


class EventInput(Input):
    key: Key
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    family: Literal["icpc", "ccpc", "provincial", "lanqiao", "gplt", "astar", "matibei", "other"]
    occurred_on: date | None = None
    date_precision: Literal["day", "month", "year"] = "day"
    category: str = Field(default="", max_length=200)


class AwardInput(Input):
    source_key: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
    ]
    event: EventInput
    award: Text
    team: str = Field(default="", max_length=200)
    source_url: HttpUrl
    evidence: str = Field(default="", max_length=5000)
    recipients: list[Text] = Field(min_length=1, max_length=30)


class ImportInput(Input):
    records: list[AwardInput] = Field(min_length=1, max_length=500)
    preview: bool = True


class EventDateInput(Input):
    occurred_on: date | None
    date_precision: Literal["day", "month", "year"]
    expected_occurred_on: date | None
    expected_date_precision: Literal["day", "month", "year"]


class ReviewInput(Input):
    revision: int
    status: Literal["pending", "verified", "excluded"]
    person_ids: list[int | None] = Field(max_length=30)


def person_view(p):
    return {
        **p.model_dump(exclude={"aliases_json"}),
        "aliases": json.loads(p.aliases_json),
        "effective_study_end": study_end(p),
    }


def lock(db):
    # Acquire SQLite's write lock even if the queue is empty; no external I/O inside.
    db.execute(update(HonorAward).where(HonorAward.id == -1).values(revision=HonorAward.revision))
    db.expire_all()


def people_map(db):
    return {p.id: p for p in db.exec(select(HonorPerson)).all()}


def award_views(db, *, admin=False, user_id=None):
    people = people_map(db)
    recipients = {}
    for r in db.exec(select(HonorRecipient).order_by(HonorRecipient.position)).all():
        recipients.setdefault(r.award_id, []).append(r)
    events = {e.id: e for e in db.exec(select(HonorEvent)).all()}
    result = []
    for a in db.exec(select(HonorAward).order_by(HonorAward.id.desc())).all():
        e = events[a.event_id]
        members = []
        for r in recipients.get(a.id, []):
            p = people.get(r.person_id)
            reason = eligibility(p, e)
            if admin or (a.status == "verified" and reason == "计入"):
                member = {"name": p.name if p else r.raw_name, "person_id": r.person_id}
                if admin:
                    member.update(raw_name=r.raw_name, eligibility=reason)
                members.append(member)
        if not admin and (a.status != "verified" or not members):
            continue
        if user_id is not None and not any(
            people.get(m["person_id"]) and people[m["person_id"]].user_id == user_id
            for m in members
        ):
            continue
        item = {
            "id": a.id,
            "event": e.model_dump(),
            "award": a.award,
            "team": a.team,
            "source_url": a.source_url,
            "recipients": members,
        }
        if admin:
            item.update(
                status=a.status, revision=a.revision, evidence=a.evidence, source_key=a.source_key
            )
        result.append(item)
    return sorted(
        result, key=lambda a: (str(a["event"]["occurred_on"] or ""), a["id"]), reverse=True
    )


@router.get("")
def public_honors(db: Session = Depends(get_session)):
    return award_views(db)


@router.get("/users/{user_id}")
def user_honors(
    user_id: int, _user: User = Depends(get_current_user), db: Session = Depends(get_session)
):
    if db.get(User, user_id) is None:
        raise HTTPException(404, "成员不存在")
    return award_views(db, user_id=user_id)


@router.get("/admin/people", dependencies=[Depends(require_admin)])
def list_people(db: Session = Depends(get_session)):
    return [person_view(p) for p in db.exec(select(HonorPerson)).all()]


def save_person(db, data, person_id=None):
    lock(db)
    person = db.get(HonorPerson, person_id) if person_id is not None else None
    if person_id is not None and person is None:
        raise HTTPException(404, "身份不存在")
    if person:
        db.refresh(person)
        if person.merged_into_id or person.revision != data.revision:
            raise HTTPException(409, "身份已更新，请重新打开后编辑")
    if data.user_id:
        if db.get(User, data.user_id) is None:
            raise HTTPException(404, "关联的站内成员不存在")
        existing = db.exec(select(HonorPerson).where(HonorPerson.user_id == data.user_id)).first()
        if existing and (not person or existing.id != person.id):
            raise HTTPException(409, "这个站内成员已关联其他身份，请核对重名或曾用名")
    p = person or HonorPerson(name=data.name)
    for k, v in data.model_dump(exclude={"aliases", "revision"}).items():
        setattr(p, k, v)
    p.aliases_json = json.dumps(list(dict.fromkeys(data.aliases)), ensure_ascii=False)
    p.revision += 1 if person else 0
    db.add(p)
    db.commit()
    db.refresh(p)
    return person_view(p)


@router.post("/admin/people", dependencies=[Depends(require_admin)])
def create_person(data: PersonInput, db: Session = Depends(get_session)):
    return save_person(db, data)


@router.put("/admin/people/{person_id}", dependencies=[Depends(require_admin)])
def update_person(person_id: int, data: PersonInput, db: Session = Depends(get_session)):
    return save_person(db, data, person_id)


@router.get("/admin/awards", dependencies=[Depends(require_admin)])
def list_awards(db: Session = Depends(get_session)):
    return award_views(db, admin=True)


@router.post("/admin/import", dependencies=[Depends(require_admin)])
def import_awards(data: ImportInput, db: Session = Depends(get_session)):
    if not data.preview:
        lock(db)
    added, duplicates, failed = [], [], []
    seen = set(db.exec(select(HonorAward.source_key)).all())
    events = {e.key: e for e in db.exec(select(HonorEvent)).all()}
    for row, record in enumerate(data.records, 1):
        key = record.source_key
        if key in seen:
            duplicates.append(key)
            continue
        event_data = record.event.model_dump()
        event = events.get(record.event.key)
        if event and any(getattr(event, k) != v for k, v in event_data.items()):
            failed.append(
                {
                    "row": row,
                    "source_key": key,
                    "reason": "赛事信息与现有记录冲突",
                    "record": record.model_dump(mode="json"),
                }
            )
            continue
        if event is None:
            event = HonorEvent(**event_data)
            events[event.key] = event
            if not data.preview:
                db.add(event)
                db.flush()
        if not data.preview:
            award = HonorAward(
                event_id=event.id,
                source_key=key,
                award=record.award,
                team=record.team,
                source_url=str(record.source_url),
                evidence=record.evidence,
            )
            db.add(award)
            db.flush()
            for position, name in enumerate(record.recipients):
                db.add(HonorRecipient(award_id=award.id, position=position, raw_name=name))
        seen.add(key)
        added.append(key)
    if not data.preview:
        db.commit()
    return {"preview": data.preview, "added": added, "duplicates": duplicates, "failed": failed}


@router.put("/admin/awards/{award_id}", dependencies=[Depends(require_admin)])
def review_award(award_id: int, data: ReviewInput, db: Session = Depends(get_session)):
    lock(db)
    award = db.get(HonorAward, award_id)
    if award is None:
        raise HTTPException(404, "记录不存在")
    if award.revision != data.revision:
        raise HTTPException(409, "记录已更新，请重新打开后编辑")
    recipients = db.exec(
        select(HonorRecipient)
        .where(HonorRecipient.award_id == award_id)
        .order_by(HonorRecipient.position)
    ).all()
    if len(recipients) != len(data.person_ids):
        raise HTTPException(422, "获奖者人数不匹配")
    assigned = [i for i in data.person_ids if i is not None]
    if len(assigned) != len(set(assigned)):
        raise HTTPException(422, "同一条获奖记录不能重复关联同一个人")
    for r, person_id in zip(recipients, data.person_ids, strict=True):
        if person_id is not None:
            p = db.get(HonorPerson, person_id)
            if p is None or p.merged_into_id:
                raise HTTPException(422, "关联身份不存在或已经合并")
        r.person_id = person_id
        db.add(r)
    award.status = data.status
    award.revision += 1
    db.add(award)
    db.commit()
    return {"id": award.id, "revision": award.revision}


@router.get("/admin/people/{person_id}/merge-preview", dependencies=[Depends(require_admin)])
def merge_preview(person_id: int, target_id: int = Query(gt=0), db: Session = Depends(get_session)):
    source, target = db.get(HonorPerson, person_id), db.get(HonorPerson, target_id)
    if not source or not target:
        raise HTTPException(404, "身份不存在")
    if person_id == target_id or source.merged_into_id or target.merged_into_id:
        raise HTTPException(422, "请选择两个尚未合并的不同身份")
    conflicts = []
    if source.user_id and target.user_id and source.user_id != target.user_id:
        conflicts.append("关联了不同登录账号")
    if (source.study_start, study_end(source)) != (target.study_start, study_end(target)):
        conflicts.append("在校区间不同")
    return {
        "source": person_view(source),
        "target": person_view(target),
        "conflicts": conflicts,
        "executable": False,
        "reason": "已预留身份合并接口；执行及审计功能尚未开放",
    }


@router.put("/admin/events/{event_id}/date", dependencies=[Depends(require_admin)])
def update_event_date(event_id: int, data: EventDateInput, db: Session = Depends(get_session)):
    lock(db)
    event = db.get(HonorEvent, event_id)
    if event is None:
        raise HTTPException(404, "赛事不存在")
    if (event.occurred_on, event.date_precision) != (
        data.expected_occurred_on,
        data.expected_date_precision,
    ):
        raise HTTPException(409, "比赛日期已更新，请重新打开后编辑")
    event.occurred_on, event.date_precision = data.occurred_on, data.date_precision
    db.add(event)
    db.commit()
    return event
