"""Versioned teaching problems and self-reported client results (local app only)."""

import json
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, select

from ..db import get_session
from ..deps import get_current_user, require_admin, require_origin_secret
from ..learning_models import LearningProblem, LearningRun, LearningVersion
from ..models import User

router = APIRouter(dependencies=[Depends(require_origin_secret)], tags=["local-learning"])
Environment = Literal["c", "cpp", "python", "java8", "java21"]


def encoded(value: BaseModel | dict) -> str:
    return json.dumps(
        value.model_dump() if isinstance(value, BaseModel) else value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class TestCase(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    input: str = Field(max_length=1_000_000)
    output: str = Field(max_length=1_000_000)
    sample: bool = False


Difficulty = Literal["入门", "基础", "进阶"]


class ProblemContent(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    chapter: str = Field(min_length=1, max_length=80)
    difficulty: Difficulty = "入门"
    tags: list[str] = Field(default_factory=list, max_length=8)
    statement: str = Field(min_length=1, max_length=40_000)
    environments: list[Environment] = Field(min_length=1, max_length=5)
    checker: Literal["exact", "tokens"] = "tokens"
    time_ms: int = Field(default=3000, ge=100, le=30_000)
    tests: list[TestCase] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def limits(self):
        if not self.title.strip() or not self.chapter.strip() or not self.statement.strip():
            raise ValueError("标题、章节和题面不能为空")
        if len(set(self.environments)) != len(self.environments):
            raise ValueError("语言不能重复")
        if len(set(self.tags)) != len(self.tags) or any(
            not t.strip() or len(t) > 30 for t in self.tags
        ):
            raise ValueError("标签不能为空或重复，单个标签最多 30 字")
            raise ValueError("语言不能重复")
        if not any(t.sample for t in self.tests):
            raise ValueError("至少需要一个样例")
        if sum(len((t.input + t.output).encode()) for t in self.tests) > 8_000_000:
            raise ValueError("测试数据合计不能超过 8 MB")
        return self


class DraftCreate(BaseModel):
    request_id: UUID
    content: ProblemContent


class DraftUpdate(BaseModel):
    revision: int = Field(ge=1)
    content: ProblemContent


class Revision(BaseModel):
    revision: int = Field(ge=1)


def problem_or_404(db, problem_id):
    problem = db.get(LearningProblem, problem_id)
    if problem is None:
        raise HTTPException(404, "题目不存在")
    return problem


def serialize_draft(p):
    return {
        "id": p.id,
        "revision": p.draft_revision,
        "published_revision": p.published_revision,
        "content": json.loads(p.draft_json),
    }


def serialize_version(v):
    return {
        "id": v.problem_id,
        "version_id": v.id,
        "revision": v.revision,
        "content": json.loads(v.content_json),
    }


@router.get("/learning/problems")
def list_problems(db: Session = Depends(get_session)):
    rows = db.exec(
        select(LearningVersion)
        .join(LearningProblem)
        .where(LearningProblem.published_revision == LearningVersion.revision)
        .order_by(LearningProblem.id)
    ).all()
    return [
        {
            "id": v.problem_id,
            "version_id": v.id,
            "revision": v.revision,
            **{
                k: json.loads(v.content_json).get(k, {"difficulty": "入门", "tags": []}.get(k))
                for k in ("title", "chapter", "difficulty", "tags", "environments")
            },
        }
        for v in rows
    ]


@router.get("/learning/problems/{problem_id}")
def get_problem(problem_id: int, db: Session = Depends(get_session)):
    p = problem_or_404(db, problem_id)
    v = db.exec(
        select(LearningVersion).where(
            LearningVersion.problem_id == p.id, LearningVersion.revision == p.published_revision
        )
    ).first()
    if v is None:
        raise HTTPException(404, "题目尚未发布")
    return serialize_version(v)


@router.get("/learning/versions/{version_id}")
def get_version(version_id: str, db: Session = Depends(get_session)):
    v = db.get(LearningVersion, version_id)
    if v is None or problem_or_404(db, v.problem_id).published_revision is None:
        raise HTTPException(404, "题目版本不存在")
    return serialize_version(v)


@router.get("/admin/learning/problems")
def admin_list(db: Session = Depends(get_session), _user: User = Depends(require_admin)):
    return [
        serialize_draft(p)
        for p in db.exec(select(LearningProblem).order_by(LearningProblem.id)).all()
    ]


@router.post("/admin/learning/problems", status_code=201)
def create_problem(
    body: DraftCreate, db: Session = Depends(get_session), _user: User = Depends(require_admin)
):
    content = encoded(body.content)
    digest = sha256(content.encode()).hexdigest()
    db.exec(
        insert(LearningProblem)
        .values(creation_id=str(body.request_id), creation_hash=digest, draft_json=content)
        .on_conflict_do_nothing(index_elements=["creation_id"])
    )
    p = db.exec(
        select(LearningProblem).where(LearningProblem.creation_id == str(body.request_id))
    ).one()
    if p.creation_hash != digest:
        raise HTTPException(409, "同一新建请求已处理，请保留原内容重试，或刷新题目列表")
    db.commit()
    return serialize_draft(p)


@router.put("/admin/learning/problems/{problem_id}")
def save_draft(
    problem_id: int,
    body: DraftUpdate,
    db: Session = Depends(get_session),
    _user: User = Depends(require_admin),
):
    # Acquire the writer before reading; a stale tab must not overwrite a newer draft.
    db.exec(
        update(LearningProblem)
        .where(LearningProblem.id == problem_id)
        .values(draft_revision=LearningProblem.draft_revision)
    )
    p = problem_or_404(db, problem_id)
    db.refresh(p)
    content = encoded(body.content)
    if body.revision != p.draft_revision:
        if content == p.draft_json:
            return serialize_draft(p)
        raise HTTPException(409, "草稿已被其他页面修改，请刷新后重试")
    if content != p.draft_json:
        p.draft_json = content
        p.draft_revision += 1
        db.add(p)
        db.commit()
        db.refresh(p)
    return serialize_draft(p)


@router.post("/admin/learning/problems/{problem_id}/publish")
def publish(
    problem_id: int,
    body: Revision,
    db: Session = Depends(get_session),
    _user: User = Depends(require_admin),
):
    db.exec(
        update(LearningProblem)
        .where(LearningProblem.id == problem_id)
        .values(draft_revision=LearningProblem.draft_revision)
    )
    p = problem_or_404(db, problem_id)
    db.refresh(p)
    if body.revision != p.draft_revision:
        raise HTTPException(409, "草稿版本已变化，请刷新后重试")
    ProblemContent.model_validate_json(p.draft_json)
    v = db.exec(
        select(LearningVersion).where(
            LearningVersion.problem_id == p.id, LearningVersion.revision == p.draft_revision
        )
    ).first()
    if v is None:
        v = LearningVersion(
            id=str(uuid4()), problem_id=p.id, revision=p.draft_revision, content_json=p.draft_json
        )
        db.add(v)
    p.published_revision = p.draft_revision
    db.add(p)
    db.flush()
    # 题单/集训从 ProblemCache 取题名与标签，发布新版本时同步刷新。
    from ..learning_helpers import upsert_learn_cache

    upsert_learn_cache(db, p)
    db.commit()
    db.refresh(v)
    return serialize_version(v)


class RunBody(BaseModel):
    id: UUID
    version_id: UUID
    environment: Environment
    runtime_version: str = Field(min_length=1, max_length=160)
    source: str = Field(max_length=256_000)
    verdict: Literal["AC", "WA", "CE", "RE", "TLE", "OLE"]


@router.post("/learning/runs")
def save_run(
    body: RunBody, db: Session = Depends(get_session), user: User = Depends(get_current_user)
):
    v = db.get(LearningVersion, str(body.version_id))
    if v is None or problem_or_404(db, v.problem_id).published_revision is None:
        raise HTTPException(404, "题目版本不存在")
    if body.environment not in json.loads(v.content_json)["environments"]:
        raise HTTPException(422, "该题不支持所选环境")
    digest = sha256(encoded(body.model_dump(mode="json")).encode()).hexdigest()
    db.exec(
        insert(LearningRun)
        .values(
            id=str(body.id),
            user_id=user.id,
            version_id=v.id,
            environment=body.environment,
            runtime_version=body.runtime_version,
            source=body.source,
            verdict=body.verdict,
            request_hash=digest,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    row = db.get(LearningRun, str(body.id))
    if row.user_id != user.id or row.request_hash != digest:
        raise HTTPException(409, "评测 ID 已被其他内容使用")
    db.commit()
    return {"id": row.id, "source": "client", "verdict": row.verdict}


@router.get("/learning/runs")
def my_runs(db: Session = Depends(get_session), user: User = Depends(get_current_user)):
    rows = db.exec(
        select(LearningRun, LearningVersion.problem_id)
        .join(LearningVersion, LearningVersion.id == LearningRun.version_id)
        .where(LearningRun.user_id == user.id)
        .order_by(LearningRun.created_at.desc())
        .limit(100)
    ).all()
    return [
        {
            "id": r.id,
            "version_id": r.version_id,
            "problem_id": problem_id,
            "environment": r.environment,
            "runtime_version": r.runtime_version,
            "verdict": r.verdict,
            "created_at": r.created_at,
            "source": "client",
        }
        for r, problem_id in rows
    ]
