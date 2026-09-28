"""FastAPI dependencies: the DB session, the login gate, and CSRF enforcement.

Everything shared across requests (the session factory, ``AppConfig``, the
adapter registry, the rate limiter) lives on ``request.app.state``, set once
when the app is built in :mod:`polska.dashboard.app`, not re-created per
request.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Form, HTTPException, Request, status
from sqlalchemy.orm import Session

from polska.config.appconfig import AppConfig
from polska.dashboard.security import csrf_token_matches

#: The session key marking a logged-in admin. Its value is not a secret in its
#: own right; the whole session cookie is already signed and, once
#: ``dashboard_cookie_secure`` is on, HTTPS-only.
_SESSION_KEY_LOGGED_IN = "admin"
_SESSION_KEY_CSRF = "csrf_token"


def get_session(request: Request) -> Iterator[Session]:
    """One DB session per request. Commits on a clean response, rolls back on
    an exception, always closes -- the same contract ``session_scope`` gives
    the scheduler process, applied per-request here instead of per-tick."""
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_app_config(request: Request) -> AppConfig:
    return request.app.state.app_config


def get_adapter_registry(request: Request):
    return request.app.state.adapter_registry


def require_login(request: Request) -> str:
    """The logged-in admin's identity, or a redirect to the login page.

    Returns a fixed string rather than nothing: every activity-log write from
    a dashboard action records who did it, and "the admin" is genuinely all
    there is to know in a single-operator system. If this ever grows real
    multi-user accounts, this is the one place that changes.
    """
    if not request.session.get(_SESSION_KEY_LOGGED_IN):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return "admin"


def csrf_token_for_session(request: Request) -> str:
    """The current session's CSRF token, creating one if this session has none
    yet (e.g. the very first page it renders a form on)."""
    token = request.session.get(_SESSION_KEY_CSRF)
    if not token:
        from polska.dashboard.security import new_csrf_token

        token = new_csrf_token()
        request.session[_SESSION_KEY_CSRF] = token
    return token


async def require_csrf(request: Request, csrf_token: str = Form(...)) -> None:
    """Refuses a state-changing request whose form did not carry the exact
    token this session was issued. Every POST route that changes anything
    depends on this, not just the login form."""
    session_token = request.session.get(_SESSION_KEY_CSRF)
    if not csrf_token_matches(session_token, csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad CSRF token.")


def mark_logged_in(request: Request) -> None:
    request.session[_SESSION_KEY_LOGGED_IN] = True


def clear_session(request: Request) -> None:
    request.session.clear()


SessionDep = Depends(get_session)
LoginDep = Depends(require_login)
CsrfDep = Depends(require_csrf)
