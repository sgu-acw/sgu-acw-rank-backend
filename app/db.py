"""Database engine and session handling.

SQLite is the only supported backend. Two pragmas matter and are applied to
every connection:

* ``journal_mode=WAL`` lets readers run while a writer holds the write lock.
  Without it the scraping job would block every API request for the duration of
  its transaction.
* ``busy_timeout`` makes a blocked writer wait instead of failing immediately
  with "database is locked".
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings

_engine: Engine | None = None


def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _configure_sqlite)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine. Used by tests that point at a fresh database."""
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None


def init_db() -> None:
    """Create any missing tables. Safe to call on every start.

    只建表不加列：已有表的新增列由 ``migrate.run_migration`` 补，
    ``create_app`` 启动时会在 init_db 之后跑它。
    """
    from . import honor_models, models  # noqa: F401  (import registers the tables)

    SQLModel.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session for background work, committed on success."""
    with Session(get_engine()) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    with Session(get_engine()) as session:
        yield session
