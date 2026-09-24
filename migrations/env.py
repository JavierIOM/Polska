"""Alembic environment.

The database URL comes from ``polska.config.settings``, not from alembic.ini, so the
migrations and the application can never point at different databases.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

# Importing the models package registers every table on Base.metadata. Without this
# import, autogenerate produces an empty migration and reports nothing wrong.
import polska.db.models  # noqa: F401
from polska.config.settings import load_settings
from polska.db.base import Base, make_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """The URL to migrate, preferring an explicit -x url= over the settings."""
    overrides = context.get_x_argument(as_dictionary=True)
    return overrides.get("url") or load_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most things in place. Batch mode rebuilds the table.
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run the migrations."""
    engine = make_engine(_database_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            poolclass=pool.NullPool,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
