"""Every dashboard route. Nine of them; one file is enough.

Read routes render a template. State-changing routes are all POST, all behind
``require_login``, and all behind ``require_csrf`` -- approve, reject, clear a
halt, write off an orphan. None of them re-derive anything: they call the one
function each already-tested action owns (``decide_approval``,
``execute_approval``, ``clear_halt``, ``write_off_orphan``) and redirect back
to where the human came from.
"""

from __future__ import annotations

import contextlib
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from polska.budget import clear_halt
from polska.dashboard.deps import (
    csrf_token_for_session,
    get_adapter_registry,
    get_app_config,
    get_session,
    mark_logged_in,
    require_csrf,
    require_login,
)
from polska.dashboard.security import verify_password
from polska.db.enums import ApprovalStatus, RunStatus
from polska.db.models import ActivityEvent, Approval, BudgetHalt, Company, Goal, Run
from polska.gate import (
    ApprovalAlreadyDecided,
    ApprovalNotDecided,
    decide_approval,
    execute_approval,
)

router = APIRouter()

_RECENT_ACTIVITY_LIMIT = 50
_RECENT_RUNS_LIMIT = 30


def _companies(session: Session) -> list[Company]:
    return list(session.execute(select(Company).order_by(Company.slug)).scalars())


def _selected_company(session: Session, slug: str | None) -> Company | None:
    companies = _companies(session)
    if not companies:
        return None
    if slug:
        for company in companies:
            if company.slug == slug:
                return company
    return companies[0]


def _redirect_with_error(path: str, message: str) -> RedirectResponse:
    """A redirect carrying an error message, properly query-encoded so it
    cannot be mis-parsed (or, worse, used to inject something) by whatever
    ends up in ``message``."""
    return RedirectResponse(
        f"{path}?{urlencode({'error': message})}", status_code=status.HTTP_303_SEE_OTHER
    )


# ------------------------------------------------------------------------------ login


@router.get("/login")
def login_form(request: Request):
    templates = request.app.state.templates
    error = request.query_params.get("error")
    return templates.TemplateResponse(
        request,
        "login.html",
        {"csrf_token": csrf_token_for_session(request), "error": error},
    )


@router.post("/login")
def login_submit(
    request: Request,
    password: str = Form(...),
    _csrf: None = Depends(require_csrf),
):
    limiter = request.app.state.rate_limiter
    client_ip = request.client.host if request.client else "unknown"

    if limiter.is_locked_out(client_ip):
        wait = limiter.seconds_until_retry(client_ip)
        return _redirect_with_error("/login", f"Too many attempts. Wait {wait}s.")

    if verify_password(request.app.state.admin_password_hash, password):
        limiter.record_success(client_ip)
        mark_logged_in(request)
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    limiter.record_failure(client_ip)
    return _redirect_with_error("/login", "Wrong password.")


@router.post("/logout")
def logout(request: Request, _: None = Depends(require_csrf)):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- dashboard


@router.get("/")
def home(
    request: Request,
    company_slug: str | None = None,
    session: Session = Depends(get_session),
    _admin: str = Depends(require_login),
):
    templates = request.app.state.templates
    companies = _companies(session)
    company = _selected_company(session, company_slug)

    goals: list[Goal] = []
    activity: list[ActivityEvent] = []
    runs: list[Run] = []
    if company is not None:
        goals = list(
            session.execute(
                select(Goal).where(Goal.company_id == company.id).order_by(Goal.priority, Goal.id)
            ).scalars()
        )
        activity = list(
            session.execute(
                select(ActivityEvent)
                .where(ActivityEvent.company_id == company.id)
                .order_by(ActivityEvent.id.desc())
                .limit(_RECENT_ACTIVITY_LIMIT)
            ).scalars()
        )
        runs = list(
            session.execute(
                select(Run)
                .where(Run.company_id == company.id)
                .order_by(Run.id.desc())
                .limit(_RECENT_RUNS_LIMIT)
            ).scalars()
        )

    pending_approvals = 0
    open_halts = 0
    if company is not None:
        pending_approvals = session.execute(
            select(Approval).where(
                Approval.company_id == company.id, Approval.status == ApprovalStatus.PENDING
            )
        ).all()
        pending_approvals = len(pending_approvals)
        open_halts = len(
            session.execute(
                select(BudgetHalt).where(
                    BudgetHalt.cleared_at.is_(None),
                    (BudgetHalt.company_id == company.id) | (BudgetHalt.company_id.is_(None)),
                )
            ).all()
        )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "companies": companies,
            "company": company,
            "goals": goals,
            "activity": activity,
            "runs": runs,
            "pending_approvals": pending_approvals,
            "open_halts": open_halts,
            "csrf_token": csrf_token_for_session(request),
        },
    )


# -------------------------------------------------------------------------- approvals


@router.get("/approvals")
def approvals_list(
    request: Request,
    company_slug: str | None = None,
    session: Session = Depends(get_session),
    _admin: str = Depends(require_login),
):
    templates = request.app.state.templates
    companies = _companies(session)
    company = _selected_company(session, company_slug)

    pending: list[Approval] = []
    decided: list[Approval] = []
    if company is not None:
        pending = list(
            session.execute(
                select(Approval)
                .options(selectinload(Approval.task))
                .where(Approval.company_id == company.id, Approval.status == ApprovalStatus.PENDING)
                .order_by(Approval.requested_at)
            ).scalars()
        )
        decided = list(
            session.execute(
                select(Approval)
                .options(selectinload(Approval.task))
                .where(Approval.company_id == company.id, Approval.status != ApprovalStatus.PENDING)
                .order_by(Approval.id.desc())
                .limit(20)
            ).scalars()
        )

    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "companies": companies,
            "company": company,
            "pending": pending,
            "decided": decided,
            "csrf_token": csrf_token_for_session(request),
        },
    )


@router.post("/approvals/{approval_id}/approve")
async def approve(
    request: Request,
    approval_id: int,
    note: str = Form(""),
    session: Session = Depends(get_session),
    app_config=Depends(get_app_config),
    registry=Depends(get_adapter_registry),
    _admin: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="No such approval.")

    try:
        decided = decide_approval(session, approval, approved=True, decided_by=_admin, note=note)
    except ApprovalAlreadyDecided as exc:
        return _redirect_with_error("/approvals", str(exc))

    with contextlib.suppress(ApprovalNotDecided):  # cannot happen: just set APPROVED above
        await execute_approval(session, app_config, registry, decided)
    return RedirectResponse("/approvals", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/approvals/{approval_id}/reject")
def reject(
    request: Request,
    approval_id: int,
    note: str = Form(""),
    session: Session = Depends(get_session),
    _admin: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="No such approval.")

    try:
        decide_approval(session, approval, approved=False, decided_by=_admin, note=note)
    except ApprovalAlreadyDecided as exc:
        return _redirect_with_error("/approvals", str(exc))
    return RedirectResponse("/approvals", status_code=status.HTTP_303_SEE_OTHER)


# ----------------------------------------------------------------------------- budget


@router.get("/budget")
def budget_page(
    request: Request,
    company_slug: str | None = None,
    session: Session = Depends(get_session),
    _admin: str = Depends(require_login),
):
    templates = request.app.state.templates
    companies = _companies(session)
    company = _selected_company(session, company_slug)

    open_halts: list[BudgetHalt] = []
    cleared_halts: list[BudgetHalt] = []
    orphans: list[Run] = []
    if company is not None:
        open_halts = list(
            session.execute(
                select(BudgetHalt)
                .options(selectinload(BudgetHalt.run))
                .where(
                    BudgetHalt.cleared_at.is_(None),
                    (BudgetHalt.company_id == company.id) | (BudgetHalt.company_id.is_(None)),
                )
                .order_by(BudgetHalt.created_at)
            ).scalars()
        )
        cleared_halts = list(
            session.execute(
                select(BudgetHalt)
                .options(selectinload(BudgetHalt.run))
                .where(
                    BudgetHalt.cleared_at.is_not(None),
                    (BudgetHalt.company_id == company.id) | (BudgetHalt.company_id.is_(None)),
                )
                .order_by(BudgetHalt.cleared_at.desc())
                .limit(20)
            ).scalars()
        )
        orphans = list(
            session.execute(
                select(Run)
                .where(Run.company_id == company.id, Run.status == RunStatus.ORPHANED)
                .order_by(Run.started_at.desc())
            ).scalars()
        )

    return templates.TemplateResponse(
        request,
        "budget.html",
        {
            "companies": companies,
            "company": company,
            "open_halts": open_halts,
            "cleared_halts": cleared_halts,
            "orphans": orphans,
            "csrf_token": csrf_token_for_session(request),
        },
    )


@router.post("/budget/halts/{halt_id}/clear")
def clear_halt_route(
    request: Request,
    halt_id: int,
    note: str = Form(""),
    session: Session = Depends(get_session),
    _admin: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    halt = session.get(BudgetHalt, halt_id)
    if halt is None:
        raise HTTPException(status_code=404, detail="No such halt.")
    try:
        clear_halt(session, halt, cleared_by=_admin, note=note)
    except ValueError as exc:
        return _redirect_with_error("/budget", str(exc))
    return RedirectResponse("/budget", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/budget/orphans/{run_id}/write-off")
def write_off_route(
    request: Request,
    run_id: int,
    actual_cost_usd: float = Form(...),
    note: str = Form(""),
    session: Session = Depends(get_session),
    _admin: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    from polska.budget import write_off_orphan

    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run.")
    try:
        write_off_orphan(
            session, run, actual_cost_usd=actual_cost_usd, decided_by=_admin, note=note
        )
    except ValueError as exc:
        return _redirect_with_error("/budget", str(exc))
    return RedirectResponse("/budget", status_code=status.HTTP_303_SEE_OTHER)
