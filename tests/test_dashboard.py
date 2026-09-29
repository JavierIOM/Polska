"""The dashboard: login, CSRF, and the four state-changing routes.

Function over polish, but the security properties are not optional: every
state-changing route must both require login and refuse a request without the
right CSRF token, and a login form must be rate-limited. Those four things are
what this file is really pinning; the HTML itself is not the point.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from polska.config.appconfig import AppConfig
from polska.config.settings import Settings
from polska.dashboard.app import create_app
from polska.dashboard.security import hash_password
from polska.db.enums import ActivityKind, ApprovalStatus, BudgetScope, RunStatus
from polska.db.models import Approval, BudgetHalt, Company, Run

PASSWORD = "correct horse battery staple"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        admin_password_hash=hash_password(PASSWORD),
        session_secret="test-session-secret-not-a-real-one",
        dashboard_cookie_secure=False,
    )


@pytest.fixture
def client(
    settings: Settings, app_config: AppConfig, session_factory: sessionmaker[Session]
) -> TestClient:
    app = create_app(settings=settings, app_config=app_config, session_factory=session_factory)
    return TestClient(app)


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token found in the page"
    return match.group(1)


def _login(client: TestClient) -> None:
    csrf = _csrf_from(client.get("/login").text)
    response = client.post(
        "/login", data={"password": PASSWORD, "csrf_token": csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# --------------------------------------------------------------------------- login


def test_the_home_page_redirects_to_login_when_not_logged_in(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_a_correct_password_logs_in(client: TestClient) -> None:
    _login(client)
    response = client.get("/")
    assert response.status_code == 200
    assert "dashboard" in response.text.lower() or "Polska" in response.text


def test_a_wrong_password_is_refused(client: TestClient) -> None:
    csrf = _csrf_from(client.get("/login").text)
    response = client.post(
        "/login", data={"password": "not it", "csrf_token": csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?error=")
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_a_login_post_with_no_csrf_token_is_refused(client: TestClient) -> None:
    response = client.post("/login", data={"password": PASSWORD, "csrf_token": "garbage"})
    assert response.status_code == 403


def test_repeated_failures_lock_out_the_login_form(client: TestClient) -> None:
    csrf = _csrf_from(client.get("/login").text)
    for _ in range(5):
        client.post("/login", data={"password": "nope", "csrf_token": csrf})

    response = client.post(
        "/login", data={"password": PASSWORD, "csrf_token": csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "Too+many+attempts" in response.headers["location"] or "attempts" in response.headers[
        "location"
    ].replace("%20", " ")
    # Locked out even with the *correct* password: the whole point.
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


# ---------------------------------------------------------------------------- csrf


def test_a_state_changing_route_with_no_csrf_token_is_refused(
    client: TestClient, session: Session, company: Company
) -> None:
    _login(client)
    approval = Approval(
        company_id=company.id,
        action_type="email.send",
        adapter="dry_run",
        payload={"to": "x@example.invalid"},
        preview="To: x@example.invalid",
        status=ApprovalStatus.PENDING,
    )
    session.add(approval)
    session.commit()

    response = client.post(f"/approvals/{approval.id}/approve", data={})
    assert response.status_code == 422  # FastAPI's own missing-required-field response


def test_a_state_changing_route_with_a_wrong_csrf_token_is_refused(
    client: TestClient, session: Session, company: Company
) -> None:
    _login(client)
    approval = Approval(
        company_id=company.id,
        action_type="email.send",
        adapter="dry_run",
        payload={"to": "x@example.invalid"},
        preview="To: x@example.invalid",
        status=ApprovalStatus.PENDING,
    )
    session.add(approval)
    session.commit()

    response = client.post(
        f"/approvals/{approval.id}/approve", data={"csrf_token": "not-the-real-token"}
    )
    assert response.status_code == 403


def test_a_state_changing_route_requires_login(client: TestClient, session: Session) -> None:
    response = client.post(
        "/approvals/1/approve", data={"csrf_token": "whatever"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# ------------------------------------------------------------------------- approvals


def test_approving_executes_the_stored_payload_and_redirects(
    client: TestClient, session: Session, company: Company
) -> None:
    _login(client)
    approval = Approval(
        company_id=company.id,
        action_type="email.send",
        adapter="dry_run",
        payload={"to": "x@example.invalid", "body": "hi"},
        preview="To: x@example.invalid\n\nhi",
        status=ApprovalStatus.PENDING,
    )
    session.add(approval)
    session.commit()

    csrf = _csrf_from(client.get("/approvals").text)
    response = client.post(
        f"/approvals/{approval.id}/approve",
        data={"csrf_token": csrf, "note": "looks fine"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/approvals"

    session.refresh(approval)
    assert approval.status == ApprovalStatus.EXECUTED
    assert approval.decided_by == "admin"
    assert approval.decision_note == "looks fine"
    assert approval.execution_result is not None


def test_rejecting_never_executes_anything(
    client: TestClient, session: Session, company: Company
) -> None:
    _login(client)
    approval = Approval(
        company_id=company.id,
        action_type="email.send",
        adapter="dry_run",
        payload={"to": "x@example.invalid"},
        preview="To: x@example.invalid",
        status=ApprovalStatus.PENDING,
    )
    session.add(approval)
    session.commit()

    csrf = _csrf_from(client.get("/approvals").text)
    client.post(
        f"/approvals/{approval.id}/reject",
        data={"csrf_token": csrf, "note": "not now"},
        follow_redirects=False,
    )

    session.refresh(approval)
    assert approval.status == ApprovalStatus.REJECTED
    assert approval.execution_result is None


def test_the_preview_text_and_not_the_raw_payload_is_what_renders(
    client: TestClient, session: Session, company: Company
) -> None:
    """decide_approval and execute_approval act on payload; the page a human
    reads before deciding must show preview. This is what proves the page
    actually renders that field, not something reconstructed from payload."""
    _login(client)
    approval = Approval(
        company_id=company.id,
        action_type="email.send",
        adapter="dry_run",
        payload={"to": "x@example.invalid", "body": "internal payload marker"},
        preview="A human-readable preview, deliberately different text",
        status=ApprovalStatus.PENDING,
    )
    session.add(approval)
    session.commit()

    html = client.get("/approvals").text
    assert "A human-readable preview, deliberately different text" in html


# ---------------------------------------------------------------------------- budget


def test_clearing_a_halt_via_the_route(
    client: TestClient, session: Session, company: Company
) -> None:
    _login(client)
    halt = BudgetHalt(
        company_id=company.id,
        scope=BudgetScope.COMPANY,
        limit_name="max_usd_per_company",
        limit_value=1.0,
        observed_value=2.0,
        reason="test halt",
    )
    session.add(halt)
    session.commit()

    csrf = _csrf_from(client.get("/budget").text)
    response = client.post(
        f"/budget/halts/{halt.id}/clear",
        data={"csrf_token": csrf, "note": "topped up"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.refresh(halt)
    assert not halt.is_active
    assert halt.cleared_by == "admin"


def test_the_run_that_caused_a_halt_is_shown(
    client: TestClient, session: Session, company: Company
) -> None:
    from polska.db.enums import AgentName

    run = Run(
        company_id=company.id,
        agent=AgentName.ANALYST,
        model="claude-sonnet-5",
        status=RunStatus.INTERRUPTED,
        cost_usd=1.23,
    )
    session.add(run)
    session.flush()
    halt = BudgetHalt(
        company_id=company.id,
        run_id=run.id,
        scope=BudgetScope.RUN,
        limit_name="mid_run_watchdog",
        limit_value=1.0,
        observed_value=1.23,
        reason=f"Run {run.id}: cut off mid-stream.",
    )
    session.add(halt)
    session.commit()

    _login(client)
    html = client.get("/budget").text
    assert f"run #{run.id}" in html.lower()


def test_a_token_ceiling_halt_is_never_shown_with_a_dollar_sign(
    client: TestClient, session: Session, company: Company
) -> None:
    """The exact incident this closes: a run-scoped max_tokens_per_run halt
    rendered as "$773927.0000 against a limit of 750000" -- a token count
    formatted as dollars. Both figures on this halt are token counts; neither
    should ever carry a $."""
    from polska.db.enums import AgentName

    run = Run(
        company_id=company.id,
        agent=AgentName.ENGINEER,
        model="claude-opus-5",
        status=RunStatus.INTERRUPTED,
        cost_usd=0.87,
        input_tokens=700_000,
        output_tokens=73_927,
    )
    session.add(run)
    session.flush()
    halt = BudgetHalt(
        company_id=company.id,
        run_id=run.id,
        scope=BudgetScope.RUN,
        limit_name="max_tokens_per_run",
        limit_value=750_000,
        observed_value=773_927,
        reason=f"Run {run.id} used 773927 tokens against a same-model safety net of 750000.",
    )
    session.add(halt)
    session.commit()

    _login(client)
    html = client.get("/budget").text

    assert "$773927" not in html
    assert "$750000" not in html
    assert "773927 tokens" in html
    assert "750000 tokens" in html


def test_writing_off_an_orphan_via_the_route(
    client: TestClient, session: Session, company: Company
) -> None:
    from polska.db.enums import AgentName

    _login(client)
    run = Run(
        company_id=company.id,
        agent=AgentName.ANALYST,
        model="claude-sonnet-5",
        status=RunStatus.ORPHANED,
        cost_usd=3.0,
    )
    session.add(run)
    session.commit()

    csrf = _csrf_from(client.get("/budget").text)
    response = client.post(
        f"/budget/orphans/{run.id}/write-off",
        data={"csrf_token": csrf, "actual_cost_usd": "0.42", "note": "confirmed via console"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.refresh(run)
    assert run.status == RunStatus.RECONCILED
    assert run.cost_usd == pytest.approx(0.42)


def test_the_activity_feed_records_who_cleared_a_halt(
    client: TestClient, session: Session, company: Company
) -> None:
    from polska.db.models import ActivityEvent

    _login(client)
    halt = BudgetHalt(
        company_id=company.id,
        scope=BudgetScope.COMPANY,
        limit_name="max_usd_per_company",
        limit_value=1.0,
        observed_value=2.0,
        reason="test halt",
    )
    session.add(halt)
    session.commit()

    csrf = _csrf_from(client.get("/budget").text)
    client.post(f"/budget/halts/{halt.id}/clear", data={"csrf_token": csrf})

    events = (
        session.execute(
            select(ActivityEvent).where(ActivityEvent.kind == ActivityKind.BUDGET_RESUMED)
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].detail["cleared_by"] == "admin"
