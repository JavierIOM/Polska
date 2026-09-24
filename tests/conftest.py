"""Shared fixtures.

Every test runs against a real SQLite database built from the models, not from the
migration. A separate test asserts the two agree, so the rest of the suite does not
pay migration cost on every case.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import polska.db.models  # noqa: F401  (registers the tables)
from polska.config.appconfig import AppConfig, load_app_config
from polska.db.base import Base, make_engine, make_session_factory
from polska.db.enums import GoalStatus, TaskType
from polska.db.models import Company, Goal, Task

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """A throwaway file-backed database.

    On disk rather than in memory, because the WAL pragma the app depends on is a
    no-op for ``:memory:`` and a test suite that never exercises it would not notice
    the real thing failing.
    """
    db = tmp_path / "test.db"
    eng = make_engine(f"sqlite+pysqlite:///{db.as_posix()}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """The sessionmaker itself, for tests that need several independent sessions
    against the same database, e.g. simulating concurrent dispatches."""
    return make_session_factory(engine)


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session bound to the throwaway database."""
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


@pytest.fixture
def app_config() -> AppConfig:
    """The real shipped config, so the tests fail if it stops being valid."""
    return load_app_config(REPO_ROOT / "config" / "default.yaml")


@pytest.fixture
def company(session: Session) -> Company:
    """A saved company with one open goal."""
    record = Company(
        slug="test-co",
        name="Test Co",
        profile_path="companies/test.yaml",
        profile_hash="0" * 64,
        profile={},
        idea="Selling things to people.",
        brand_voice="Plain.",
    )
    session.add(record)
    session.flush()

    goal = Goal(
        company_id=record.id,
        title="Grow the list",
        metric="subscribers",
        target_value=500,
        current_value=62,
        unit="subscribers",
        status=GoalStatus.ACTIVE,
    )
    session.add(goal)
    session.commit()
    return record


@pytest.fixture
def task(session: Session, company: Company) -> Task:
    """A saved task sitting in ``queued``."""
    record = Task(
        company_id=company.id,
        goal_id=company.goals[0].id,
        type=TaskType.MARKETING,
        title="Draft the launch announcement",
        description="Write the post announcing the new range.",
        rationale="The range is live and nothing has been said about it.",
    )
    session.add(record)
    session.commit()
    return record
