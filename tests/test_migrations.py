"""The migration and the models must describe the same schema.

The rest of the suite builds its database with ``create_all`` for speed. That is only
safe while the migration agrees with the models, which is what this asserts.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect

import polska.db.models  # noqa: F401  (registers the tables)
from polska.db.base import Base, make_engine

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Engine:
    """A database built by running every migration from scratch."""
    db = tmp_path / "migrated.db"
    url = f"sqlite+pysqlite:///{db.as_posix()}"

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    # env.py reads the target through get_x_argument, which is populated from
    # cmd_opts. This is how the CLI passes -x, so it is what the fixture mimics.
    config.cmd_opts = Namespace(x=[f"url={url}"])
    command.upgrade(config, "head")

    return make_engine(url)


def test_the_migration_creates_every_model_table(migrated_engine: Engine) -> None:
    migrated = set(inspect(migrated_engine).get_table_names()) - {"alembic_version"}
    assert migrated == set(Base.metadata.tables)


def test_every_column_matches(migrated_engine: Engine) -> None:
    """Column names per table, both directions, so neither side may drift alone."""
    inspector = inspect(migrated_engine)
    for name, table in Base.metadata.tables.items():
        migrated = {column["name"] for column in inspector.get_columns(name)}
        declared = set(table.columns.keys())
        assert migrated == declared, f"{name} columns differ"


def test_every_index_matches(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    for name, table in Base.metadata.tables.items():
        migrated = {index["name"] for index in inspector.get_indexes(name)}
        declared = {index.name for index in table.indexes}
        assert migrated == declared, f"{name} indexes differ"


def test_foreign_keys_survive_the_migration(migrated_engine: Engine) -> None:
    """A dangling task_id should be an error, which needs the FK to actually exist."""
    inspector = inspect(migrated_engine)
    task_fks = {fk["referred_table"] for fk in inspector.get_foreign_keys("tasks")}
    assert task_fks == {"companies", "goals"}

    run_fks = {fk["referred_table"] for fk in inspector.get_foreign_keys("runs")}
    assert run_fks == {"companies", "tasks"}


def test_the_connection_runs_in_wal_with_foreign_keys_on(migrated_engine: Engine) -> None:
    """Neither is a SQLite default, and the app depends on both."""
    with migrated_engine.connect() as connection:
        journal = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert journal == "wal"
    assert foreign_keys == 1
