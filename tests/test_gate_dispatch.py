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
from polska.gate import ApprovalNotDecided, dispatch_action, execute_approval
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
