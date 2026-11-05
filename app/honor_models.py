"""Honor identities are independent of login accounts; names are never unique keys."""

from datetime import date

from sqlmodel import Field, SQLModel, UniqueConstraint


class HonorPerson(SQLModel, table=True):
    __tablename__ = "honor_people"
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=100)
    aliases_json: str = "[]"
    user_id: int | None = Field(default=None, foreign_key="users.id", unique=True)
    study_start: date | None = None
    study_end: date | None = None  # exclusive; NULL means start + four calendar years
    note: str = ""
    revision: int = 1
    # Reserved for a future audited identity merge, never a login-account merge.
    merged_into_id: int | None = Field(default=None, foreign_key="honor_people.id")


class HonorEvent(SQLModel, table=True):
    __tablename__ = "honor_events"
    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(unique=True, max_length=200)
    title: str = Field(max_length=300)
    family: str = Field(max_length=40)
    occurred_on: date | None = None
    date_precision: str = "day"
    category: str = Field(default="", max_length=200)


class HonorAward(SQLModel, table=True):
    __tablename__ = "honor_awards"
    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="honor_events.id", index=True)
    source_key: str = Field(unique=True, max_length=300)
    award: str = Field(max_length=100)
    team: str = Field(default="", max_length=200)
    source_url: str
    evidence: str = ""
    status: str = "pending"  # pending / verified / excluded
    revision: int = 1


class HonorRecipient(SQLModel, table=True):
    __tablename__ = "honor_recipients"
    __table_args__ = (UniqueConstraint("award_id", "position"),)
    id: int | None = Field(default=None, primary_key=True)
    award_id: int = Field(foreign_key="honor_awards.id", index=True)
    position: int
    raw_name: str = Field(max_length=100)
    person_id: int | None = Field(default=None, foreign_key="honor_people.id", index=True)
