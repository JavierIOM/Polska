"""Refuses to start against a database that migrations have not caught up to.

Found the hard way: a database copied forward from a previous version let both
the scheduler and dashboard containers start clean, and only the dashboard
failed, once it happened to query a column that did not exist yet. The
scheduler would have ticked against the same stale schema in 24 hours,
whatever that failure would have looked like.

This project's own rule (``main.py``'s docstring) is that migrations are a
deliberate, separate, human-run step -- ``alembic upgrade head`` -- never
something a container does to itself on boot; an app that quietly migrates
itself is its own kind of footgun. The fix for a stale schema is therefore not
to run migrations automatically here too, but to make staleness loud and
immediate instead of silent and delayed: refuse to start at all, with the
exact command to fix it, rather than starting "successfully" and failing on
whatever code path happens to touch the missing column first.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine


class SchemaOutOfDate(RuntimeError):
    """The database's alembic revision does not match the migrations on disk."""


def assert_schema_is_current(
    engine: Engine, *, alembic_ini_path: str | Path = "alembic.ini"
) -> None:
    """Raise :class:`SchemaOutOfDate` if the database is not at the migrations'
    head revision. Call once, at process start, before anything else touches
    the database.

    ``alembic_ini_path`` is a plain relative path by default, resolved against
    the process's cwd, the same fix and the same reasoning as
    ``Settings.config_path`` and its siblings: correct in both dev (repo root)
    and the container (``WORKDIR /app``, where the Dockerfile ``COPY``'s
    ``alembic.ini`` to exactly that path), without depending on an env var.
    """
    config = Config(str(alembic_ini_path))
    script = ScriptDirectory.from_config(config)
    head_revisions = set(script.get_heads())

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        current_revisions = set(context.get_current_heads())

    if current_revisions == head_revisions:
        return

    raise SchemaOutOfDate(
        "The database is not at the migrations' head revision. This process "
        "refuses to start against it rather than run with a schema it does "
        "not match and fail unpredictably later, possibly hours or days from "
        "now on whatever code path first touches the difference.\n"
        f"  Database is at: {sorted(current_revisions) or '(no migrations applied at all)'}\n"
        f"  Migrations' head is: {sorted(head_revisions)}\n"
        "Run the migration, then start this again:\n"
        "  docker compose run --rm scheduler alembic upgrade head"
    )
