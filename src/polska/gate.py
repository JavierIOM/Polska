"""The approval gate.

One entry point, :func:`dispatch_action`, is where every proposed external effect
from an agent's :class:`AgentResult` arrives. From here on the rule is simple:

- **Reversible** actions run immediately. No ``Approval`` row is created for them;
  the ``Approval`` table is specifically for actions awaiting a yes or no, and a
  reversible action never asks. The activity feed is their only record.
- **Irreversible** actions always get an ``Approval`` row. If config auto-approves
  that action type, it is executed right away and the row records that a human never
  saw it. Otherwise it is parked ``pending`` and nothing happens until a decision
  arrives, in phase 4, from the dashboard.

:func:`execute_approval` is the one function that ever calls an adapter with a stored
payload. It is used here for the auto-approve path and is written to be the same
function the dashboard's approve button will call: it takes only the ``Approval``
row, replays its payload exactly as stored, and never consults the agent that
proposed it again.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from polska.activity import log
from polska.adapters.registry import AdapterRegistry
from polska.config.appconfig import AppConfig
from polska.db.enums import ActivityKind, ApprovalStatus, Reversibility
from polska.db.models import Approval
from polska.db.types import utcnow
from polska.schemas.actions import ActionRequest


class ApprovalNotDecided(Exception):
    """Raised if execution is attempted on an approval nobody has approved yet."""


class ApprovalAlreadyDecided(Exception):
    """Raised if a decision is attempted on an approval that is not pending
    (already decided, or its window has passed)."""


async def dispatch_action(
    session: Session,
    app_config: AppConfig,
    registry: AdapterRegistry,
    *,
    company_id: int,
    task_id: int | None,
    run_id: int | None,
    action: ActionRequest,
) -> Approval | None:
    """Classify one proposed action and either run it or park it.

    Returns the created :class:`Approval` for an irreversible action (whatever its
    resulting status), or ``None`` for a reversible one that ran directly and has no
    row of its own.
    """
    if not app_config.is_irreversible(action.action_type):
        log(
            session,
            company_id=company_id,
            kind=ActivityKind.ACTION_PROPOSED,
            summary=action.summary,
            detail={
                "action_type": action.action_type,
                "adapter": action.adapter,
                "reversibility": Reversibility.REVERSIBLE.value,
            },
            task_id=task_id,
            run_id=run_id,
        )
        await _run_adapter(
            session,
            app_config,
            registry,
            action_type=action.action_type,
            adapter_name=action.adapter,
            payload=action.payload,
            company_id=company_id,
            task_id=task_id,
            run_id=run_id,
            approval=None,
        )
        return None

    approval = Approval(
        company_id=company_id,
        task_id=task_id,
        run_id=run_id,
        action_type=action.action_type,
        adapter=action.adapter,
        reversibility=Reversibility.IRREVERSIBLE,
        payload=action.payload,
        preview=action.preview,
        status=ApprovalStatus.PENDING,
    )
    session.add(approval)
    session.flush()

    # Logged before any execution is attempted, whether that happens in the next
    # line (auto-approved) or days from now from the dashboard. This is the "an
    # external effect is logged before it is attempted" moment for every irreversible
    # action, regardless of how long it waits.
    log(
        session,
        company_id=company_id,
        kind=ActivityKind.ACTION_PROPOSED,
        summary=action.summary,
        detail={
            "action_type": action.action_type,
            "adapter": action.adapter,
            "reversibility": Reversibility.IRREVERSIBLE.value,
        },
        task_id=task_id,
        run_id=run_id,
        approval_id=approval.id,
    )

    if app_config.may_auto_approve(action.action_type):
        approval.status = ApprovalStatus.APPROVED
        approval.auto_approved = True
        approval.decided_by = "system:auto_approve"
        approval.decided_at = utcnow()
        session.flush()
        log(
            session,
            company_id=company_id,
            kind=ActivityKind.APPROVAL_DECIDED,
            summary=f"Auto-approved by config: {action.action_type}",
            detail={"decided_by": "system:auto_approve"},
            task_id=task_id,
            run_id=run_id,
            approval_id=approval.id,
        )
        return await execute_approval(session, app_config, registry, approval)

    approval.expires_at = utcnow() + dt.timedelta(hours=app_config.approvals.expiry_hours)
    session.commit()
    return approval


def decide_approval(
    session: Session,
    approval: Approval,
    *,
    approved: bool,
    decided_by: str,
    note: str = "",
) -> Approval:
    """Record a human's yes or no. Never executes anything itself.

    Deliberately split from :func:`execute_approval`: a rejection has nothing to
    run, and even an approval's own execution is a second, separate step the
    caller chooses to take next (the dashboard's approve route calls this, then
    :func:`execute_approval`, in that order). ``decided_by`` is required, never
    defaulted, so every decision has a name attached the same way every other
    deliberate human act in this system does.

    Refuses a decision on anything but a still-open ``PENDING`` approval,
    including one whose ``expires_at`` has quietly passed: rather than silently
    honouring a stale decision, this is what actually moves it to ``EXPIRED`` the
    first time anyone looks at it, since nothing else in the system sweeps for
    that on its own.
    """
    now = utcnow()
    if approval.status == ApprovalStatus.PENDING and approval.is_expired(now):
        approval.status = ApprovalStatus.EXPIRED
        session.commit()
        log(
            session,
            company_id=approval.company_id,
            kind=ActivityKind.APPROVAL_DECIDED,
            summary=f"Expired before a decision arrived: {approval.action_type}",
            task_id=approval.task_id,
            run_id=approval.run_id,
            approval_id=approval.id,
        )
        raise ApprovalAlreadyDecided(
            f"Approval {approval.id} expired at {approval.expires_at} before this "
            "decision was recorded."
        )

    if approval.status != ApprovalStatus.PENDING:
        raise ApprovalAlreadyDecided(
            f"Approval {approval.id} is already {approval.status.value}, not pending."
        )

    approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    approval.decided_by = decided_by
    approval.decided_at = now
    approval.decision_note = note or None
    session.commit()

    log(
        session,
        company_id=approval.company_id,
        kind=ActivityKind.APPROVAL_DECIDED,
        summary=f"{'Approved' if approved else 'Rejected'} by {decided_by}: {approval.action_type}",
        detail={"decided_by": decided_by, "note": note},
        task_id=approval.task_id,
        run_id=approval.run_id,
        approval_id=approval.id,
    )
    return approval


async def execute_approval(
    session: Session,
    app_config: AppConfig,
    registry: AdapterRegistry,
    approval: Approval,
) -> Approval:
    """Replay a decided approval's stored payload, exactly as it was written.

    Requires ``approval.status == APPROVED``. This is the one function a dashboard
    approve button (phase 4) will call, and it takes nothing from the caller but the
    approval itself: it never re-derives the payload, never asks the agent again, and
    never looks at anything that could have changed since the action was proposed.
    """
    if approval.status != ApprovalStatus.APPROVED:
        raise ApprovalNotDecided(
            f"Approval {approval.id} is {approval.status}, not approved. "
            "execute_approval only replays a decision that has already been made."
        )
    return await _run_adapter(
        session,
        app_config,
        registry,
        action_type=approval.action_type,
        adapter_name=approval.adapter,
        payload=approval.payload,
        company_id=approval.company_id,
        task_id=approval.task_id,
        run_id=approval.run_id,
        approval=approval,
    )


async def _run_adapter(
    session: Session,
    app_config: AppConfig,
    registry: AdapterRegistry,
    *,
    action_type: str,
    adapter_name: str,
    payload: dict[str, object],
    company_id: int,
    task_id: int | None,
    run_id: int | None,
    approval: Approval | None,
) -> Approval | None:
    """Call the adapter and record the outcome. The only place that happens.

    ``force_dry_run`` substitutes the adapter actually called, without touching what
    was proposed or stored: the record still says what the agent asked for, and the
    substitution itself is written into the outcome so nobody mistakes a dry run for
    the real thing later.
    """
    effective_name = "dry_run" if app_config.integrations.force_dry_run else adapter_name
    adapter = registry.get(effective_name)

    try:
        result = await adapter.execute(action_type, payload)
    except Exception as exc:  # noqa: BLE001 - an adapter crash is data, not a bug here
        if approval is not None:
            approval.status = ApprovalStatus.EXECUTION_FAILED
            approval.execution_error = str(exc)
            session.commit()
        log(
            session,
            company_id=company_id,
            kind=ActivityKind.ERROR,
            summary=f"Adapter {effective_name!r} raised while executing {action_type}",
            detail={"action_type": action_type, "adapter": effective_name},
            task_id=task_id,
            run_id=run_id,
            approval_id=approval.id if approval else None,
            error=str(exc),
        )
        return approval

    # The adapter's own receipt, flattened rather than the whole AdapterResult
    # wrapper: ``succeeded`` and ``error`` are already tracked on the Approval's own
    # ``status`` and ``execution_error``, so nesting them again here would just be
    # two copies of the same fact that can drift from each other.
    detail = dict(result.detail)
    if effective_name != adapter_name:
        detail["intercepted_by_force_dry_run"] = True
        detail["proposed_adapter"] = adapter_name

    if approval is not None:
        approval.executed_at = utcnow()
        approval.execution_result = detail
        if result.succeeded:
            approval.status = ApprovalStatus.EXECUTED
        else:
            approval.status = ApprovalStatus.EXECUTION_FAILED
            approval.execution_error = result.error or "adapter reported failure"
        session.commit()

    log(
        session,
        company_id=company_id,
        kind=ActivityKind.ACTION_EXECUTED,
        summary=f"{action_type} via {effective_name}: {'ok' if result.succeeded else 'failed'}",
        detail={"action_type": action_type, "adapter": effective_name, **detail},
        task_id=task_id,
        run_id=run_id,
        approval_id=approval.id if approval else None,
        error=None if result.succeeded else result.error,
    )
    return approval
