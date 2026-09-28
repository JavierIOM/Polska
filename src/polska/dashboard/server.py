"""The dashboard's process entrypoint: builds the app, runs it under uvicorn.

    python -m polska.dashboard.server

A separate process from ``polska.main`` (the scheduler) on purpose: one crash
must not take the other down, and they scale differently (the scheduler is a
single background loop, the dashboard serves whoever is looking at it). They
share the same database file, nothing else.
"""

from __future__ import annotations

import logging

import uvicorn

from polska.adapters.registry import AdapterRegistry
from polska.config.appconfig import load_app_config
from polska.config.settings import load_settings
from polska.dashboard.app import create_app
from polska.db.base import make_engine, make_session_factory
from polska.db.schema_check import assert_schema_is_current


def build_app():
    settings = load_settings()
    logging.basicConfig(level=settings.log_level)
    app_config = load_app_config(settings.config_path)
    engine = make_engine(settings.database_url, echo=settings.sql_echo)
    # Before anything else touches the database: a schema behind head must
    # stop this process outright, not run degraded until a page queries the
    # difference. See schema_check.py for the incident this closes.
    assert_schema_is_current(engine)
    session_factory = make_session_factory(engine)
    return create_app(
        settings=settings,
        app_config=app_config,
        session_factory=session_factory,
        adapter_registry=AdapterRegistry(),
    )


def run() -> None:
    settings = load_settings()
    # A single worker, deliberately: the login rate limiter and CSRF tokens
    # both live in this process's memory (see security.py), and the scheduler
    # already assumes one writer to the SQLite file at a time. Passing the
    # app instance directly rather than an import string means no --reload
    # and no multi-worker mode, which is exactly the tradeoff wanted here.
    uvicorn.run(
        build_app(),
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
