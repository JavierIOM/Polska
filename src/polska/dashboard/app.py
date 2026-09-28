"""Builds the FastAPI app. One factory, called once per process by
:mod:`polska.dashboard.server` (and once per test by ``tests/test_dashboard.py``,
against an isolated in-memory database).

Function over polish: plain server-rendered HTML via Jinja2, no frontend build
step, no JS framework. A handful of forms and tables is all this needs to be.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from polska.adapters.registry import AdapterRegistry
from polska.config.appconfig import AppConfig
from polska.config.settings import Settings
from polska.dashboard.security import LoginRateLimiter

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(
    *,
    settings: Settings,
    app_config: AppConfig,
    session_factory: sessionmaker,
    adapter_registry: AdapterRegistry | None = None,
) -> FastAPI:
    """Build the dashboard app against an already-constructed session factory,
    so tests can point it at a throwaway SQLite database and production code
    points it at the same one the scheduler writes to."""
    admin_hash, session_secret = settings.require_dashboard_secrets()

    app = FastAPI(title="Polska", docs_url=None, redoc_url=None)

    # Shared, request-independent state. Dependencies in deps.py read these
    # off request.app.state rather than closing over them, so the same app
    # object always reflects exactly what was passed in here.
    app.state.settings = settings
    app.state.app_config = app_config
    app.state.session_factory = session_factory
    app.state.adapter_registry = adapter_registry or AdapterRegistry()
    app.state.admin_password_hash = admin_hash
    app.state.rate_limiter = LoginRateLimiter()

    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="polska_session",
        max_age=settings.session_max_age_seconds,
        same_site="lax",
        https_only=settings.dashboard_cookie_secure,
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["money"] = _money
    app.state.templates = templates

    from polska.dashboard.routes import router

    app.include_router(router)
    return app


def _money(value: Any) -> str:
    """``$0.00000`` for a run's own cost figures. Five places, not two: several
    real runs this project has measured cost well under a cent, and rounding
    to $0.00 would make every cheap run look free."""
    try:
        return f"${float(value):.5f}"
    except (TypeError, ValueError):
        return str(value)
