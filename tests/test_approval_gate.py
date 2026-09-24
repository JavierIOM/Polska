"""The approval gate, as far as phase 1 builds it.

The executing half of the gate lands in phase 4. What exists now is the part that
decides whether something needs approval at all, plus the record that holds it, and
those are worth pinning before anything is built on top of them.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from polska.config.appconfig import AppConfig, ApprovalConfig
from polska.db.enums import ApprovalStatus, Reversibility
from polska.db.models import Approval, Company, Task
from polska.schemas.actions import ActionRequest, AgentResult

# --------------------------------------------------------------- classification

IRREVERSIBLE = [
    "email.send",
    "social.publish",
    "git.push_default_branch",
    "payment.charge",
    "contact.third_party",
]


@pytest.mark.parametrize("action_type", IRREVERSIBLE)
def test_the_five_named_effects_are_irreversible(app_config: AppConfig, action_type: str) -> None:
    """The brief names these five explicitly. They must never execute directly."""
    assert app_config.is_irreversible(action_type)


def test_the_shipped_config_lists_exactly_those_five(app_config: AppConfig) -> None:
    assert sorted(app_config.approvals.irreversible_actions) == sorted(IRREVERSIBLE)


def test_an_unknown_action_type_fails_closed(app_config: AppConfig) -> None:
    """A new agent inventing an action name must stop at the gate, not sail past it."""
    assert app_config.approvals.unknown_action_is_irreversible
    assert app_config.is_irreversible("something.nobody_classified")


def test_unknown_actions_pass_only_when_fail_closed_is_switched_off(
    app_config: AppConfig,
) -> None:
    """Switching the guard off is the only way an unclassified action executes."""
    relaxed = app_config.model_copy(
        update={
            "approvals": app_config.approvals.model_copy(
                update={"unknown_action_is_irreversible": False}
            )
        }
    )
    assert not relaxed.is_irreversible("something.nobody_classified")
    # The named five are still caught on their own merits, not by the fallback.
    assert relaxed.is_irreversible("email.send")


def test_auto_approve_ships_empty(app_config: AppConfig) -> None:
    """The default must be that nothing acts on the world unasked."""
    assert app_config.approvals.auto_approve == []
    for action_type in IRREVERSIBLE:
        assert not app_config.may_auto_approve(action_type)


def test_auto_approve_honours_an_explicit_entry() -> None:
    config = ApprovalConfig(auto_approve=["social.publish"])
    assert config.auto_approve == ["social.publish"]


def test_auto_approving_an_unclassified_action_is_refused() -> None:
    """Listing a reversible action here does nothing, so it is a mistake worth catching."""
    with pytest.raises(ValidationError, match="not in irreversible_actions"):
        ApprovalConfig(auto_approve=["file.write"])


def test_expiry_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        ApprovalConfig(expiry_hours=0)


# --------------------------------------------------------------- action requests


def _action(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "action_type": "email.send",
        "adapter": "dry_run",
        "payload": {"to": "someone@example.invalid", "subject": "Hi", "body": "Hello."},
        "preview": "To: someone@example.invalid\nSubject: Hi\n\nHello.",
        "summary": "Email someone@example.invalid about the new range.",
    }
    base.update(overrides)
    return base


def test_a_well_formed_action_request_validates() -> None:
    request = ActionRequest.model_validate(_action())
    assert request.action_type == "email.send"
    assert request.payload["to"] == "someone@example.invalid"


def test_an_action_type_must_be_dotted() -> None:
    """The classifier matches on dotted types, so a bare word can never be classified."""
    with pytest.raises(ValidationError):
        ActionRequest.model_validate(_action(action_type="send"))


def test_an_action_request_needs_a_preview() -> None:
    """Approving without seeing what you approve is the failure this gate exists to stop."""
    with pytest.raises(ValidationError):
        ActionRequest.model_validate(_action(preview=""))


@pytest.mark.parametrize(
    "key", ["token", "api_key", "apikey", "secret", "password", "credential", "API-KEY"]
)
def test_a_credential_in_the_payload_is_refused(key: str) -> None:
    """Payloads are stored in SQLite and rendered on the dashboard. No secrets."""
    with pytest.raises(ValidationError, match="looks like a credential"):
        ActionRequest.model_validate(_action(payload={key: "sk-ant-whatever"}))


def test_extra_fields_on_an_action_request_are_refused() -> None:
    with pytest.raises(ValidationError):
        ActionRequest.model_validate(_action(execute_now=True))


# ------------------------------------------------------------------ agent results


def test_a_result_may_carry_no_actions() -> None:
    result = AgentResult(succeeded=True, summary="Drafted the post, nothing to send.")
    assert not result.has_actions


def test_a_result_carrying_actions_reports_them() -> None:
    result = AgentResult(
        succeeded=True,
        summary="Drafted and queued the announcement.",
        actions=[ActionRequest.model_validate(_action())],
    )
    assert result.has_actions
    assert result.actions[0].adapter == "dry_run"


def test_a_failed_result_must_explain_itself() -> None:
    with pytest.raises(ValidationError, match="failure_reason"):
        AgentResult(succeeded=False, summary="It did not work.")


def test_a_result_cannot_both_succeed_and_give_a_failure_reason() -> None:
    with pytest.raises(ValidationError, match="Pick one"):
        AgentResult(
            succeeded=True,
            summary="Done.",
            failure_reason="Although the publish step was skipped.",
        )


# ---------------------------------------------------------------- approval record


def _approval(company: Company, task: Task, **overrides: object) -> Approval:
    record = Approval(
        company_id=company.id,
        task_id=task.id,
        action_type="email.send",
        adapter="dry_run",
        payload={"to": "someone@example.invalid", "body": "Hello."},
        preview="To: someone@example.invalid\n\nHello.",
    )
    for name, value in overrides.items():
        setattr(record, name, value)
    return record


def test_a_new_approval_is_pending_and_irreversible(
    session: Session, company: Company, task: Task
) -> None:
    record = _approval(company, task)
    session.add(record)
    session.commit()

    assert record.status == ApprovalStatus.PENDING
    assert record.reversibility == Reversibility.IRREVERSIBLE
    assert record.is_pending
    assert not record.is_closed
    assert not record.auto_approved
    assert record.executed_at is None
    assert record.execution_result is None


@pytest.mark.parametrize(
    "status",
    [
        ApprovalStatus.REJECTED,
        ApprovalStatus.EXECUTED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.CANCELLED,
    ],
)
def test_closed_statuses_report_closed(
    session: Session, company: Company, task: Task, status: ApprovalStatus
) -> None:
    record = _approval(company, task, status=status)
    session.add(record)
    session.commit()

    assert record.is_closed
    assert not record.is_pending


def test_approved_but_not_yet_executed_is_neither_pending_nor_closed(
    session: Session, company: Company, task: Task
) -> None:
    """The window where the payload is committed to but has not run yet."""
    record = _approval(company, task, status=ApprovalStatus.APPROVED)
    session.add(record)
    session.commit()

    assert not record.is_pending
    assert not record.is_closed


def test_expiry_is_only_reached_once_the_window_passes(
    session: Session, company: Company, task: Task
) -> None:
    now = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.UTC)
    record = _approval(company, task, expires_at=now + dt.timedelta(hours=1))
    session.add(record)
    session.commit()

    assert not record.is_expired(now)
    assert record.is_expired(now + dt.timedelta(hours=2))


def test_an_approval_with_no_expiry_never_expires(
    session: Session, company: Company, task: Task
) -> None:
    record = _approval(company, task, expires_at=None)
    session.add(record)
    session.commit()

    assert not record.is_expired(dt.datetime(2030, 1, 1, tzinfo=dt.UTC))


def test_the_payload_survives_the_database_intact(
    session: Session, company: Company, task: Task
) -> None:
    """Approving replays the stored payload verbatim, so it has to round-trip exactly."""
    payload = {
        "to": "someone@example.invalid",
        "subject": "Pound signs and accents: £49, café",
        "attachments": [],
        "nested": {"reply_to": None, "count": 3},
    }
    record = _approval(company, task, payload=payload)
    session.add(record)
    session.commit()
    session.expire_all()

    reloaded = session.get(Approval, record.id)
    assert reloaded is not None
    assert reloaded.payload == payload
