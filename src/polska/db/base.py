"""Engine, session factory and the declarative base.

SQLite is run in WAL mode with foreign keys on. Neither is the default, and both are
needed: WAL so the dashboard can read while the orchestrator writes, foreign keys so a
dangling ``task_id`` is an error rather than a mystery.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for every Polska table."""

    def __repr__(self) -> str:
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


@event.listens_for(Engine, "connect")
def _configure_sqlite(dbapi_connection: Any, connection_record: Any) -> None:
    """Apply the pragmas SQLite needs to behave under a scheduler plus a web app."""
    # Only meaningful for SQLite. Other drivers have no ``execute`` on a bare cursor
    # for these statements, so guard on the module name.
    module = type(dbapi_connection).__module__
    if "sqlite" not in module:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # A writer holds the lock briefly during a tick. Wait rather than fail.
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def make_engine(url: str, *, echo: bool = False) -> Engine:
    """Build an engine for ``url``."""
    from sqlalchemy import create_engine

    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        # The scheduler and FastAPI share a process but not a thread.
        connect_args["check_same_thread"] = False
    return create_engine(url, echo=echo, future=True, connect_args=connect_args)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a session factory bound to ``engine``."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on clean exit, roll back on anything else."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
