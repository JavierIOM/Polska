"""The dispatch half of the approval gate: what happens once an action is classified.

test_approval_gate.py covers classification and the Approval record's own lifecycle.
This file covers the part that only exists from phase 2: actually calling an adapter,
and the three paths an action can take once it is classified (run now, auto-approve
and run, or park and wait).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from polska.adapters.base import AdapterResult
from polska.adapters.dry_run import DryRunAdapter
from polska.adapters.registry import AdapterRegistry, UnknownAdapter
from polska.config.appconfig import AppConfig
from polska.db.enums import ActivityKind, ApprovalStatus
from polska.db.models import ActivityEvent, Approval, Company, Task
from polska.gate import (
    ApprovalAlreadyDecided,
    ApprovalNotDecided,
    decide_approval,
    dispatch_action,
    execute_approval,
)
from polska.schemas.actions import ActionRequest


def _action(**overrides: object) -> ActionRequest:
    base: dict[str, object] = {
        "action_type": "email.send",
        "adapter": "dry_run",
        "payload": {"to": "someone@example.invalid", "body": "Hello."},
        "preview": "To: someone@example.invalid\n\nHello.",
        "summary": "Email someone about the new range.",
    }
    base.update(overrides)
    return ActionRequest.model_validate(base)


@pytest.fixture
def registry() -> AdapterRegistry:
    return AdapterRegistry()


@pytest.fixture
def permissive_config(app_config: AppConfig) -> AppConfig:
    """Fail-closed is the shipped default: an action type not on the irreversible
    list is still treated as irreversible, on the view that a made-up action name
    should stop at the gate rather than sail through it (see test_approval_gate.py).
    Exercising the genuinely-reversible path needs a config that opts out of that,
    the way a deployment would once it has a real, deliberately-reversible action
    type to configure."""
    return app_config.model_copy(
        update={
            "approvals": app_config.approvals.model_copy(
                update={"unknown_action_is_irreversible": False}
            )
        }
    )


def _feed(session: Session) -> list[ActivityEvent]:
    return list(session.execute(select(ActivityEvent).order_by(ActivityEvent.id)).scalars())


# ------------------------------------------------------------------------- reversible


async def test_a_reversible_action_runs_immediately_with_no_approval_row(
    session: Session, permissive_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    action = _action(action_type="file.write", adapter="dry_run")
    result = await dispatch_action(
        session,
        permissive_config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    assert result is None
    assert session.execute(select(Approval)).scalar_one_or_none() is None


async def test_a_reversible_action_logs_proposed_then_executed_in_order(
    session: Session, permissive_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    action = _action(action_type="file.write", adapter="dry_run")
    await dispatch_action(
        session,
        permissive_config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    kinds = [e.kind for e in _feed(session)]
    assert kinds == [ActivityKind.ACTION_PROPOSED, ActivityKind.ACTION_EXECUTED]


# ----------------------------------------------------------------------- irreversible


async def test_an_irreversible_action_is_parked_pending_by_default(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company, task: Task
) -> None:
    action = _action(action_type="email.send")
    approval = await dispatch_action(
        session,
        app_config,
        registry,
        company_id=company.id,
        task_id=task.id,
        run_id=None,
        action=action,
    )
    assert approval is not None
    assert approval.is_pending
    assert not approval.auto_approved
    assert approval.expires_at is not None
    assert approval.execution_result is None


async def test_a_pending_approval_never_touches_the_adapter(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    """The whole point of the gate: nothing runs until a decision arrives."""
    action = _action(action_type="email.send")
    await dispatch_action(
        session,
        app_config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    kinds = [e.kind for e in _feed(session)]
    assert kinds == [ActivityKind.ACTION_PROPOSED]  # no ACTION_EXECUTED


async def test_an_auto_approved_action_type_executes_immediately(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    config = app_config.model_copy(
        update={
            "approvals": app_config.approvals.model_copy(update={"auto_approve": ["email.send"]})
        }
    )
    action = _action(action_type="email.send")
    approval = await dispatch_action(
        session,
        config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    assert approval is not None
    assert approval.status == ApprovalStatus.EXECUTED
    assert approval.auto_approved
    assert approval.decided_by == "system:auto_approve"
    assert approval.executed_at is not None
    assert approval.execution_result is not None


async def test_auto_approval_logs_the_decision_before_the_execution(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    config = app_config.model_copy(
        update={
            "approvals": app_config.approvals.model_copy(update={"auto_approve": ["email.send"]})
        }
    )
    action = _action(action_type="email.send")
    await dispatch_action(
        session,
        config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    kinds = [e.kind for e in _feed(session)]
    assert kinds == [
        ActivityKind.ACTION_PROPOSED,
        ActivityKind.APPROVAL_DECIDED,
        ActivityKind.ACTION_EXECUTED,
    ]


# ---------------------------------------------------------------------- force_dry_run


async def test_force_dry_run_overrides_the_proposed_adapter(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    """The agent proposed 'github'. force_dry_run must still redirect execution to
    dry_run, and say so in the record, without rewriting what was proposed."""
    assert app_config.integrations.force_dry_run  # the shipped default
    config = app_config.model_copy(
        update={
            "approvals": app_config.approvals.model_copy(
                update={"auto_approve": ["git.push_default_branch"]}
            )
        }
    )
    action = _action(
        action_type="git.push_default_branch", adapter="github", payload={"sha": "abc123"}
    )
    approval = await dispatch_action(
        session,
        config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    assert approval is not None
    assert approval.adapter == "github"  # stored exactly as proposed
    assert approval.execution_result["intercepted_by_force_dry_run"] is True
    assert approval.execution_result["proposed_adapter"] == "github"
    assert approval.status == ApprovalStatus.EXECUTED


async def test_without_force_dry_run_an_unregistered_adapter_fails_loudly(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    """No 'github' adapter exists yet. That is a configuration bug, not a failed
    execution, so it must raise rather than be swallowed into an EXECUTION_FAILED
    row that looks like the same thing as a real adapter refusing the call."""
    config = app_config.model_copy(
        update={
            "integrations": app_config.integrations.model_copy(update={"force_dry_run": False}),
            "approvals": app_config.approvals.model_copy(
                update={"auto_approve": ["git.push_default_branch"]}
            ),
        }
    )
    action = _action(action_type="git.push_default_branch", adapter="github")
    with pytest.raises(UnknownAdapter, match="github"):
        await dispatch_action(
            session,
            config,
            registry,
            company_id=company.id,
            task_id=None,
            run_id=None,
            action=action,
        )


# -------------------------------------------------------------------------- decision


async def test_deciding_approved_lets_execute_approval_then_run_it(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    """The dashboard's approve route: decide, then execute, as two separate
    calls, in that order. This is what proves that sequence actually works."""
    action = _action(action_type="email.send")
    approval = await dispatch_action(
        session,
        app_config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    decided = decide_approval(session, approval, approved=True, decided_by="javier")
    assert decided.status == ApprovalStatus.APPROVED
    assert decided.decided_by == "javier"
    assert decided.decided_at is not None

    result = await execute_approval(session, app_config, registry, decided)
    assert result.status == ApprovalStatus.EXECUTED


async def test_deciding_rejected_never_touches_the_adapter(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    action = _action(action_type="email.send")
    approval = await dispatch_action(
        session,
        app_config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    decided = decide_approval(
        session, approval, approved=False, decided_by="javier", note="Wrong tone."
    )
    assert decided.status == ApprovalStatus.REJECTED
    assert decided.decision_note == "Wrong tone."
    assert decided.execution_result is None
    kinds = [e.kind for e in _feed(session)]
    assert ActivityKind.ACTION_EXECUTED not in kinds


async def test_deciding_an_already_decided_approval_is_refused(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    action = _action(action_type="email.send")
    approval = await dispatch_action(
        session,
        app_config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    decide_approval(session, approval, approved=True, decided_by="javier")
    with pytest.raises(ApprovalAlreadyDecided):
        decide_approval(session, approval, approved=False, decided_by="javier")


async def test_deciding_an_expired_pending_approval_expires_it_instead(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    """Nothing else in the system sweeps for a passed expires_at. This is the
    first place that actually looks, so a stale decision is refused rather
    than silently honoured."""
    import datetime as dt

    action = _action(action_type="email.send")
    approval = await dispatch_action(
        session,
        app_config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    approval.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    session.commit()

    with pytest.raises(ApprovalAlreadyDecided):
        decide_approval(session, approval, approved=True, decided_by="javier")
    assert approval.status == ApprovalStatus.EXPIRED


# ------------------------------------------------------------------------- execution


async def test_execute_approval_refuses_anything_not_approved(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    action = _action(action_type="email.send")
    approval = await dispatch_action(
        session,
        app_config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    assert approval.status == ApprovalStatus.PENDING
    with pytest.raises(ApprovalNotDecided):
        await execute_approval(session, app_config, registry, approval)


async def test_execute_approval_replays_the_stored_payload_exactly(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    """This is the contract: approving runs what was stored, nothing recomputed."""
    action = _action(action_type="email.send", payload={"to": "x@example.invalid", "body": "hi"})
    approval = await dispatch_action(
        session,
        app_config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    approval.status = ApprovalStatus.APPROVED
    approval.decided_by = "javier"
    session.commit()

    result = await execute_approval(session, app_config, registry, approval)
    assert result.status == ApprovalStatus.EXECUTED
    assert result.execution_result["would_have_sent"] == {"to": "x@example.invalid", "body": "hi"}


async def test_an_adapter_reporting_failure_marks_execution_failed(
    session: Session, app_config: AppConfig, company: Company
) -> None:
    class AlwaysFails(DryRunAdapter):
        name = "dry_run"

        async def execute(self, action_type: str, payload: dict[str, Any]) -> AdapterResult:
            return AdapterResult(succeeded=False, error="the pretend API said no")

    registry = AdapterRegistry()
    registry.register(AlwaysFails())
    # Auto-approved so the adapter is actually reached in this call, rather than
    # parking pending and never touching it. See test_a_pending_approval_never_
    # touches_the_adapter for the case where it should not be reached.
    config = app_config.model_copy(
        update={
            "approvals": app_config.approvals.model_copy(update={"auto_approve": ["email.send"]})
        }
    )

    action = _action(action_type="email.send")
    approval = await dispatch_action(
        session,
        config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    assert approval.status == ApprovalStatus.EXECUTION_FAILED
    assert approval.execution_error == "the pretend API said no"


async def test_an_adapter_that_raises_marks_execution_failed_and_logs_an_error(
    session: Session, app_config: AppConfig, company: Company
) -> None:
    class Explodes(DryRunAdapter):
        name = "dry_run"

        async def execute(self, action_type: str, payload: dict[str, Any]) -> AdapterResult:
            raise RuntimeError("the pretend API is on fire")

    registry = AdapterRegistry()
    registry.register(Explodes())
    config = app_config.model_copy(
        update={
            "approvals": app_config.approvals.model_copy(update={"auto_approve": ["email.send"]})
        }
    )

    action = _action(action_type="email.send")
    approval = await dispatch_action(
        session,
        config,
        registry,
        company_id=company.id,
        task_id=None,
        run_id=None,
        action=action,
    )
    assert approval.status == ApprovalStatus.EXECUTION_FAILED
    assert "on fire" in approval.execution_error

    kinds = [e.kind for e in _feed(session)]
    assert ActivityKind.ERROR in kinds


# --------------------------------------------------- a task leaves awaiting_approval


async def _parked_task(session: Session, app_config: AppConfig, registry, company, *types):
    """A task in awaiting_approval with one pending approval per action type."""
    from polska.db.enums import TaskState, TaskType

    task = Task(company_id=company.id, type=TaskType.ENGINEERING, title="Ship it", rationale="x")
    session.add(task)
    session.flush()
    task.transition_to(TaskState.RUNNING)
    approvals = [
        await dispatch_action(
            session,
            app_config,
            registry,
            company_id=company.id,
            task_id=task.id,
            run_id=None,
            action=_action(action_type=action_type),
        )
        for action_type in types
    ]
    task.transition_to(
        TaskState.AWAITING_APPROVAL, result={"summary": "did it", "output": {"a": 1}}
    )
    session.commit()
    return task, approvals


async def test_approving_and_executing_every_action_completes_the_task(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    """7 Oct 2026: both approvals executed, yet the tasks stayed awaiting_approval, which
    counts against max_concurrent_tasks, so dispatch stopped for good."""
    from polska.db.enums import TaskState

    task, (first, second) = await _parked_task(
        session, app_config, registry, company, "email.send", "social.publish"
    )

    await execute_approval(
        session,
        app_config,
        registry,
        decide_approval(session, first, approved=True, decided_by="javier"),
    )
    assert task.state == TaskState.AWAITING_APPROVAL  # one still pending

    await execute_approval(
        session,
        app_config,
        registry,
        decide_approval(session, second, approved=True, decided_by="javier"),
    )
    assert task.state == TaskState.DONE
    assert task.result["summary"] == "did it"


async def test_a_rejection_abandons_the_task(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    from polska.db.enums import TaskState

    task, (approval,) = await _parked_task(session, app_config, registry, company, "email.send")

    decide_approval(session, approval, approved=False, decided_by="javier")

    assert task.state == TaskState.ABANDONED
    assert "rejected" in task.result["abandoned_because"]
    assert task.result["summary"] == "did it"


async def test_an_approved_action_that_fails_to_execute_fails_the_task(
    session: Session, app_config: AppConfig, company: Company
) -> None:
    from polska.db.enums import TaskState

    class Broken(DryRunAdapter):
        async def execute(self, action_type: str, payload: dict[str, Any]) -> AdapterResult:
            return AdapterResult(succeeded=False, error="remote said no")

    registry = AdapterRegistry()
    registry.register(Broken())
    task, (approval,) = await _parked_task(session, app_config, registry, company, "email.send")

    await execute_approval(
        session,
        app_config,
        registry,
        decide_approval(session, approval, approved=True, decided_by="javier"),
    )

    assert task.state == TaskState.FAILED
    assert "remote said no" in task.error


async def test_the_tick_sweep_expires_overdue_approvals_and_settles_their_tasks(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    """approvals.expiry_hours only took effect if someone happened to open the approval;
    nothing swept, so an unanswered task held a concurrency slot forever."""
    import datetime as dt

    from polska.db.enums import TaskState
    from polska.gate import resolve_waiting_tasks

    task, (approval,) = await _parked_task(session, app_config, registry, company, "email.send")
    approval.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    session.commit()

    assert resolve_waiting_tasks(session) == 1
    assert approval.status == ApprovalStatus.EXPIRED
    assert task.state == TaskState.ABANDONED


async def test_the_tick_sweep_settles_a_task_whose_approvals_were_already_executed(
    session: Session, app_config: AppConfig, registry: AdapterRegistry, company: Company
) -> None:
    """The live case: tasks 26 and 27 were approved and executed before this existed."""
    from polska.db.enums import TaskState
    from polska.gate import resolve_waiting_tasks

    task, (approval,) = await _parked_task(session, app_config, registry, company, "email.send")
    approval.status = ApprovalStatus.EXECUTED
    session.commit()

    assert resolve_waiting_tasks(session) == 1
    assert task.state == TaskState.DONE
