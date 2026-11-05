"""Public honors and admin review with provisional user-name matching."""

import json
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, model_validator
from sqlalchemy import update
from sqlmodel import Session, select

from ..db import get_session
from ..deps import get_current_user, require_admin, require_origin_secret
from ..honor_models import HonorAward, HonorEvent, HonorRecipient
from ..honors import eligibility, match_candidates, name_index, study_start, user_view
from ..models import User

router = APIRouter(prefix="/honors", tags=["honors"], dependencies=[Depends(require_origin_secret)])
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Key = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserProfileInput(Input):
    former_names: list[Text] = Field(default_factory=list, max_length=30)
    study_start: date | None = None
    study_end: date | None = None
    revision: int = Field(ge=1)

    @model_validator(mode="after")
    def dates(self):
        if self.study_start and self.study_start.year > 9995:
            raise ValueError("入学日期超出支持范围")
        if self.study_end and self.study_start and self.study_end <= self.study_start:
            raise ValueError("离校日期必须晚于入学日期")
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
    user_ids: list[int | None] = Field(max_length=30)


def lock(db):
    # Acquire SQLite's write lock even if the queue is empty; no external I/O inside.
    db.execute(update(HonorAward).where(HonorAward.id == -1).values(revision=HonorAward.revision))
    db.expire_all()


def award_views(db, *, admin=False, user_id=None):
    users = {u.id: u for u in db.exec(select(User)).all()}
    index = name_index(list(users.values())) if admin else {}
    recipients = {}
    for r in db.exec(select(HonorRecipient).order_by(HonorRecipient.position)).all():
        recipients.setdefault(r.award_id, []).append(r)
    events = {e.id: e for e in db.exec(select(HonorEvent)).all()}
    result = []
    for a in db.exec(select(HonorAward).order_by(HonorAward.id.desc())).all():
        e = events[a.event_id]
        members = []
        for r in recipients.get(a.id, []):
            p = users.get(r.user_id)
            reason = eligibility(p, e)
            if admin or (
                a.status == "verified" and (p is None or reason == "计入")
            ):
                member = {"name": p.display_name if p else r.raw_name}
                if admin:
                    candidates, suggested = match_candidates(r.raw_name, e, users, index)
                    member.update(
                        raw_name=r.raw_name,
                        eligibility=reason,
                        user_id=r.user_id,
                        candidates=candidates,
                        suggested_user_id=suggested,
                    )
                members.append(member)
        if not admin and (a.status != "verified" or not members):
            continue
        if user_id is not None and not any(
            r.user_id == user_id and eligibility(users.get(r.user_id), e) == "计入"
            for r in recipients.get(a.id, [])
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


@router.get("/admin/users", dependencies=[Depends(require_admin)])
def list_users(db: Session = Depends(get_session)):
    users = db.exec(select(User).order_by(User.enroll_year.desc(), User.student_id)).all()
    return [user_view(u) for u in users]


@router.get("/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def get_user_profile(user_id: int, db: Session = Depends(get_session)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "成员不存在")
    return user_view(user)


@router.put("/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def update_user_profile(user_id: int, data: UserProfileInput, db: Session = Depends(get_session)):
    lock(db)
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "成员不存在")
    if user.honor_revision != data.revision:
        raise HTTPException(409, "用户获奖资料已更新，请重新打开后编辑")
    user.honor_study_start = data.study_start
    user.honor_study_end = data.study_end
    start = study_start(user)
    if data.study_end and (not start or data.study_end <= start):
        raise HTTPException(422, "离校日期必须晚于入学日期，请核对默认或填写的入学日期")
    user.former_names_json = json.dumps(list(dict.fromkeys(data.former_names)), ensure_ascii=False)
    user.honor_revision += 1
    db.add(user)
    db.commit()
    db.refresh(user)
    return user_view(user)


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
    if len(recipients) != len(data.user_ids):
        raise HTTPException(422, "获奖者人数不匹配")
    assigned = [i for i in data.user_ids if i is not None]
    if len(assigned) != len(set(assigned)):
        raise HTTPException(422, "同一条获奖记录不能重复关联同一个人")
    for r, user_id in zip(recipients, data.user_ids, strict=True):
        if user_id is not None and db.get(User, user_id) is None:
            raise HTTPException(422, "关联用户不存在")
        r.user_id = user_id
        r.person_id = None
        db.add(r)
    award.status = data.status
    award.revision += 1
    db.add(award)
    db.commit()
    return {"id": award.id, "revision": award.revision}


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
