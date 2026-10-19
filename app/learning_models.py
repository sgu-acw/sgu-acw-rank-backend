"""Tables imported only by the isolated learning development application."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from .models import utcnow


class LearningProblem(SQLModel, table=True):
    __tablename__ = "learning_problems"
    id: int | None = Field(default=None, primary_key=True)
    creation_id: str = Field(default_factory=lambda: str(uuid4()), unique=True, index=True)
    creation_hash: str = ""
    draft_json: str
    draft_revision: int = 1
    published_revision: int | None = None
    created_at: datetime = Field(default_factory=utcnow)


class LearningVersion(SQLModel, table=True):
    __tablename__ = "learning_versions"
    __table_args__ = (UniqueConstraint("problem_id", "revision"),)
    id: str = Field(primary_key=True)
    problem_id: int = Field(foreign_key="learning_problems.id", ondelete="CASCADE", index=True)
    revision: int
    content_json: str
    created_at: datetime = Field(default_factory=utcnow)


class LearningRun(SQLModel, table=True):
    __tablename__ = "learning_runs"
    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    version_id: str = Field(foreign_key="learning_versions.id", ondelete="CASCADE", index=True)
    environment: str
    runtime_version: str
    source: str
    verdict: str
    request_hash: str
    created_at: datetime = Field(default_factory=utcnow)
