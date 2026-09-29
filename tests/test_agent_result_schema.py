"""AgentResult's own validator: succeeded, failed, or blocked, never a mix.

Pure schema tests, no database, no runner. See test_runner.py for the state
machine's side of the blocked outcome.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from polska.schemas.actions import AgentResult


def _result(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"succeeded": True, "summary": "did the thing"}
    base.update(overrides)
    return base


def test_a_success_needs_neither_reason() -> None:
    result = AgentResult.model_validate(_result())
    assert result.succeeded
    assert not result.is_environment_blocked


def test_a_failure_needs_a_failure_reason() -> None:
    result = AgentResult.model_validate(
        _result(succeeded=False, failure_reason="ran out of ideas")
    )
    assert not result.succeeded
    assert not result.is_environment_blocked


def test_a_block_needs_a_blocked_reason() -> None:
    result = AgentResult.model_validate(
        _result(succeeded=False, blocked_reason="no runtime available")
    )
    assert not result.succeeded
    assert result.is_environment_blocked


def test_a_failure_with_no_reason_at_all_is_refused() -> None:
    with pytest.raises(ValidationError, match="cannot be retried"):
        AgentResult.model_validate(_result(succeeded=False))


def test_a_failure_cannot_carry_both_reasons_at_once() -> None:
    with pytest.raises(ValidationError, match="only one can"):
        AgentResult.model_validate(
            _result(
                succeeded=False,
                failure_reason="tried and it broke",
                blocked_reason="also no runtime",
            )
        )


def test_a_success_cannot_carry_a_failure_reason() -> None:
    with pytest.raises(ValidationError, match="Pick one"):
        AgentResult.model_validate(_result(succeeded=True, failure_reason="but also this"))


def test_a_success_cannot_carry_a_blocked_reason() -> None:
    with pytest.raises(ValidationError, match="Pick one"):
        AgentResult.model_validate(_result(succeeded=True, blocked_reason="but also this"))
