"""The agent runner, against a fake SDK rather than the network.

``query_fn`` is dependency-injected specifically so these tests never import a real
API key or spend a token: each one hands the runner a small async generator built
from the real ``claude_agent_sdk`` message dataclasses, exactly as the actual SDK
would yield them, and checks what the runner does with that stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLIConnectionError,
    ProcessError,
    ResultError,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from sqlalchemy import select

from polska.adapters.registry import AdapterRegistry
from polska.budget import BudgetGuard
from polska.config.appconfig import AppConfig
from polska.config.company import CompanyProfile
from polska.db.enums import AgentName, ApprovalStatus, BudgetScope, RunStatus, TaskState
from polska.db.models import Approval, BudgetHalt, Company, Run, Task
from polska.runner import AgentRunner


def _profile(**overrides: object) -> CompanyProfile:
    base: dict[str, object] = {
        "slug": "test-co",
        "name": "Test Co",
        "idea": "Selling things to people.",
        "goals": [],
    }
    base.update(overrides)
    return CompanyProfile.model_validate(base)


def _result_message(
    *,
    is_error: bool = False,
    structured_output: Any = None,
    model_usage: dict[str, dict[str, Any]] | None = None,
    result_text: str | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype="success" if not is_error else "error_during_execution",
        duration_ms=1234,
        duration_api_ms=1000,
        is_error=is_error,
        num_turns=1,
        session_id="sess-1",
        structured_output=structured_output,
        model_usage=model_usage,
        result=result_text,
    )


def _fake_query(*messages: object):
    """Builds a ``query_fn`` that replays a fixed sequence, ignoring its args."""

    async def fake(*, prompt: str, options: object) -> AsyncIterator[object]:
        for message in messages:
            yield message

    return fake


def _fake_query_raising(exc: Exception, *messages: object):
    """A ``query_fn`` that yields some messages, then raises, mirroring how
    ``ResultError`` surfaces from the real SDK: often after a ResultMessage was
    already yielded."""

    async def fake(*, prompt: str, options: object) -> AsyncIterator[object]:
        for message in messages:
            yield message
        raise exc

    return fake


@pytest.fixture
def registry() -> AdapterRegistry:
    return AdapterRegistry()


@pytest.fixture
def budget_guard(app_config: AppConfig) -> BudgetGuard:
    return BudgetGuard(app_config)


PLANNER_USAGE = {"claude-sonnet-5": {"inputTokens": 500, "outputTokens": 100, "costUSD": 0.0021}}


# ------------------------------------------------------------------------- planner


async def test_a_successful_planner_run_validates_and_writes_a_run_row(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    output = {"tasks": [], "no_action_reason": "Nothing new since last cycle."}
    result_msg = _result_message(structured_output=output, model_usage=PLANNER_USAGE)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query(
            AssistantMessage(content=[TextBlock(text="thinking...")], model="claude-sonnet-5"),
            result_msg,
        ),
    )

    outcome = await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert outcome.output is not None
    assert outcome.output.is_empty
    assert outcome.run.status == RunStatus.SUCCEEDED
    assert outcome.run.input_tokens == 500
    assert outcome.run.output_tokens == 100
    assert outcome.run.cost_usd == pytest.approx(0.0021)
    assert outcome.run.agent == AgentName.PLANNER
    assert outcome.run.session_id == "sess-1"


async def test_the_configured_tool_list_actually_restricts_availability(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
    task: Task,
) -> None:
    """The bug this closes: this file set ``allowed_tools`` (auto-approval
    only, moot under bypassPermissions anyway) and never ``tools`` (what the
    SDK's own subprocess_cli.py only ever turns into the CLI's ``--tools``
    flag), so every agent ran with the CLI's full default toolset regardless
    of its configured allowlist for the six days since this file was first
    written. ``options.tools`` must equal the agent's configured list, not
    just ``options.allowed_tools``, so a regression here fails a test
    instead of six days passing unnoticed again."""
    captured: dict[str, object] = {}

    async def fake(*, prompt: str, options: object):
        captured["options"] = options
        yield _result_message(
            structured_output={"succeeded": True, "summary": "done", "output": {}}
        )

    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=fake,
    )

    await runner.run_worker(
        session,
        agent_name=AgentName.ENGINEER,
        task=task,
        company_profile=_profile(),
        user_prompt="Do the thing.",
    )

    configured = list(app_config.agents[AgentName.ENGINEER].tools)
    assert configured, "the fixture config must give the engineer a non-empty allowlist"
    assert captured["options"].tools == configured
    assert captured["options"].allowed_tools == configured


async def test_missing_structured_output_is_invalid_output_not_a_crash(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    result_msg = _result_message(structured_output=None, model_usage=PLANNER_USAGE)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query(result_msg),
    )

    outcome = await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert outcome.output is None
    assert outcome.run.status == RunStatus.INVALID_OUTPUT
    assert "structured_output" in outcome.run.error


async def test_structured_output_that_fails_schema_validation_is_invalid_output(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    """The CLI honouring --json-schema is not the same guarantee as Pydantic
    actually validating it. This proves the second check is load-bearing."""
    bad_output = {"tasks": [], "no_action_reason": ""}  # empty plan with no reason
    result_msg = _result_message(structured_output=bad_output, model_usage=PLANNER_USAGE)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query(result_msg),
    )

    outcome = await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert outcome.output is None
    assert outcome.run.status == RunStatus.INVALID_OUTPUT
    # The Run row still records real usage: the model was called and tokens were
    # spent even though the answer it gave was not usable.
    assert outcome.run.input_tokens == 500


async def test_a_cli_reported_error_result_is_recorded_as_failed(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    result_msg = _result_message(is_error=True, result_text="API Error: overloaded")
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query(result_msg),
    )

    outcome = await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert outcome.output is None
    assert outcome.run.status == RunStatus.FAILED
    assert "overloaded" in outcome.run.error


async def test_a_result_error_exception_is_recorded_as_failed(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    exc = ResultError("hit max turns", data={"result": "ran out of turns"})
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query_raising(exc),
    )

    outcome = await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert outcome.output is None
    assert outcome.run.status == RunStatus.FAILED
    assert "max turns" in outcome.run.error


async def test_a_timeout_is_recorded_as_timed_out_not_failed(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    async def hangs(*, prompt: str, options: object):
        import asyncio

        await asyncio.sleep(10)
        yield _result_message()  # pragma: no cover - never reached

    config = app_config.model_copy(
        update={
            "agents": {
                **app_config.agents,
                AgentName.PLANNER: app_config.agents[AgentName.PLANNER].model_copy(
                    update={"timeout_seconds": 1}
                ),
            }
        }
    )
    runner = AgentRunner(
        app_config=config, budget_guard=budget_guard, adapter_registry=registry, query_fn=hangs
    )

    outcome = await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert outcome.run.status == RunStatus.TIMED_OUT


async def test_a_cli_connection_error_is_recorded_then_reraised(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    """This is not 'this task failed', it is 'the install is broken'. It must still
    leave a record, but it must also propagate rather than be swallowed as if it
    were an ordinary per-task failure."""
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query_raising(CLIConnectionError("cli not found")),
    )

    with pytest.raises(CLIConnectionError):
        await runner.run_planner(
            session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
        )

    run = session.execute(select(Run)).scalar_one()
    assert run.status == RunStatus.FAILED


# --------------------------------------------------------------------------- worker


async def test_a_worker_with_no_actions_moves_the_task_to_done(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
    task: Task,
) -> None:
    output = {"succeeded": True, "summary": "Drafted the post.", "output": {"draft": "hello"}}
    result_msg = _result_message(structured_output=output, model_usage=PLANNER_USAGE)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query(result_msg),
    )

    await runner.run_worker(
        session,
        agent_name=AgentName.MARKETER,
        task=task,
        company_profile=_profile(),
        user_prompt="Draft the launch post.",
    )

    session.refresh(task)
    assert task.state == TaskState.DONE
    assert task.result["summary"] == "Drafted the post."
    assert task.attempts == 1


async def test_a_worker_reporting_failure_moves_the_task_to_failed(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
    task: Task,
) -> None:
    output = {"succeeded": False, "summary": "Could not draft it.", "failure_reason": "no context"}
    result_msg = _result_message(structured_output=output, model_usage=PLANNER_USAGE)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query(result_msg),
    )

    await runner.run_worker(
        session,
        agent_name=AgentName.MARKETER,
        task=task,
        company_profile=_profile(),
        user_prompt="Draft the launch post.",
    )

    session.refresh(task)
    assert task.state == TaskState.FAILED
    assert task.error == "no context"


async def test_a_worker_reporting_an_environment_block_moves_the_task_to_blocked(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
    task: Task,
) -> None:
    """Distinct from an ordinary failure: found live, an engineer task with a
    genuinely narrow scope burned most of its run ceiling because the container
    had no runtime to verify a TypeScript change, and instead of reporting that,
    it improvised a workaround. This is the path that lets the next one report
    it instead."""
    output = {
        "succeeded": False,
        "summary": "Cannot verify this change.",
        "blocked_reason": "No Node/npm runtime available to run the test suite.",
    }
    result_msg = _result_message(structured_output=output, model_usage=PLANNER_USAGE)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query(result_msg),
    )

    await runner.run_worker(
        session,
        agent_name=AgentName.ENGINEER,
        task=task,
        company_profile=_profile(),
        user_prompt="Implement the signal.",
    )

    session.refresh(task)
    assert task.state == TaskState.BLOCKED
    assert task.is_terminal
    assert task.result["blocked_reason"] == "No Node/npm runtime available to run the test suite."
    # Not routed through fail_or_abandon: attempts is not what decides this.
    assert task.attempts == 1


async def test_a_worker_proposing_an_irreversible_action_parks_the_task(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
    task: Task,
) -> None:
    """The task must not be marked done while an action it depends on is still
    sitting in the approval queue."""
    output = {
        "succeeded": True,
        "summary": "Drafted and ready to send.",
        "actions": [
            {
                "action_type": "email.send",
                "adapter": "dry_run",
                "payload": {"to": "someone@example.invalid"},
                "preview": "To: someone@example.invalid",
                "summary": "Send the drafted email.",
            }
        ],
    }
    result_msg = _result_message(structured_output=output, model_usage=PLANNER_USAGE)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query(result_msg),
    )

    await runner.run_worker(
        session,
        agent_name=AgentName.SUPPORT,
        task=task,
        company_profile=_profile(),
        user_prompt="Reply to the customer.",
    )

    session.refresh(task)
    assert task.state == TaskState.AWAITING_APPROVAL
    approval = session.execute(select(Approval)).scalar_one()
    assert approval.status == ApprovalStatus.PENDING
    assert approval.task_id == task.id


async def test_an_invalid_worker_output_moves_the_task_to_failed(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
    task: Task,
) -> None:
    result_msg = _result_message(structured_output=None, model_usage=PLANNER_USAGE)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query(result_msg),
    )

    await runner.run_worker(
        session,
        agent_name=AgentName.ANALYST,
        task=task,
        company_profile=_profile(),
        user_prompt="Research this.",
    )

    session.refresh(task)
    assert task.state == TaskState.FAILED


async def test_tool_use_is_recorded_with_its_error_flag(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
    task: Task,
) -> None:
    output = {"succeeded": True, "summary": "Looked into it.", "output": {}}
    tool_call = ToolUseBlock(id="tu_1", name="Read", input={"file_path": "notes.md"})
    tool_result = ToolResultBlock(tool_use_id="tu_1", content="file contents", is_error=False)
    result_msg = _result_message(structured_output=output, model_usage=PLANNER_USAGE)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query(
            AssistantMessage(content=[tool_call], model="claude-sonnet-5"),
            UserMessage(content=[tool_result]),
            result_msg,
        ),
    )

    outcome = await runner.run_worker(
        session,
        agent_name=AgentName.ANALYST,
        task=task,
        company_profile=_profile(),
        user_prompt="Research this.",
    )

    assert outcome.run.tools_called == [
        {"id": "tu_1", "name": "Read", "input": {"file_path": "notes.md"}, "is_error": False}
    ]


# --------------------------------------------------------------------------- budget


async def test_a_run_blocked_by_budget_never_calls_the_sdk(
    session, app_config: AppConfig, registry: AdapterRegistry, company: Company, task: Task
) -> None:
    # A token ceiling can't be forced below max_tokens_per_run (config refuses that
    # combination by construction), so the dollar ceiling is what is starved here:
    # a fresh company with no prior spend still can't afford even one reservation
    # against a near-zero daily dollar cap.
    tight = app_config.model_copy(
        update={"budget": app_config.budget.model_copy(update={"max_usd_per_day": 0.0000001})}
    )
    guard = BudgetGuard(tight)
    calls: list[object] = []

    def tracking_query(*, prompt: str, options: object):
        calls.append(options)

        async def gen():
            yield _result_message()

        return gen()

    runner = AgentRunner(
        app_config=tight, budget_guard=guard, adapter_registry=registry, query_fn=tracking_query
    )

    await runner.run_worker(
        session,
        agent_name=AgentName.MARKETER,
        task=task,
        company_profile=_profile(),
        user_prompt="Draft something.",
    )

    assert calls == []  # the SDK was never invoked
    session.refresh(task)
    assert task.state == TaskState.FAILED
    assert task.error is not None and "ceiling" in task.error.lower()


async def test_a_blocked_reservation_never_reaches_running_state_in_the_ledger(
    session, app_config: AppConfig, registry: AdapterRegistry, company: Company, task: Task
) -> None:
    """The budget_blocked row is written directly, since the SDK is never called
    and there is no async gap for a crash to land in: it must never sit around
    looking like an in-flight run that reconcile_orphaned_runs would need to
    recover."""
    tight = app_config.model_copy(
        update={"budget": app_config.budget.model_copy(update={"max_usd_per_day": 0.0000001})}
    )
    guard = BudgetGuard(tight)
    runner = AgentRunner(
        app_config=tight,
        budget_guard=guard,
        adapter_registry=registry,
        query_fn=_fake_query(_result_message()),
    )

    await runner.run_worker(
        session,
        agent_name=AgentName.MARKETER,
        task=task,
        company_profile=_profile(),
        user_prompt="Draft something.",
    )

    run = session.execute(select(Run)).scalar_one()
    assert run.status == RunStatus.BUDGET_BLOCKED


async def test_the_run_row_exists_in_running_state_before_the_sdk_is_ever_called(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    """The fix for the crash case: a Run row must be visible in the database in
    'running' state before the SDK call happens, not only after it returns. This is
    what lets reconcile_orphaned_runs find real spend a dead process never got to
    record, instead of a silent gap."""
    seen_mid_call: list[RunStatus] = []

    def spying_query(*, prompt: str, options: object):
        # At this point the runner must already have committed the row.
        row = session.execute(select(Run)).scalar_one()
        seen_mid_call.append(row.status)

        async def gen():
            yield _result_message(structured_output={"tasks": [], "no_action_reason": "none"})

        return gen()

    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=spying_query,
    )

    await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert seen_mid_call == [RunStatus.RUNNING]
    # And exactly one row exists afterwards too: the same row was updated in place,
    # not superseded by a second one.
    final_row = session.execute(select(Run)).scalar_one()
    assert final_row.status == RunStatus.SUCCEEDED


async def test_a_cli_connection_error_updates_the_existing_row_not_a_second_one(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query_raising(CLIConnectionError("cli not found")),
    )

    with pytest.raises(CLIConnectionError):
        await runner.run_planner(
            session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
        )

    rows = session.execute(select(Run)).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == RunStatus.FAILED


# --------------------------------------------------------------------- retries


async def test_a_failure_with_attempts_remaining_stays_failed_not_abandoned(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
    task: Task,
) -> None:
    assert app_config.limits.max_attempts > 1
    output = {"succeeded": False, "summary": "Nope.", "failure_reason": "transient error"}
    result_msg = _result_message(structured_output=output, model_usage=PLANNER_USAGE)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query(result_msg),
    )

    await runner.run_worker(
        session,
        agent_name=AgentName.MARKETER,
        task=task,
        company_profile=_profile(),
        user_prompt="Draft something.",
    )

    session.refresh(task)
    assert task.state == TaskState.FAILED
    assert task.attempts < app_config.limits.max_attempts


async def test_exhausting_retries_abandons_the_task_instead_of_leaving_it_failed(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
    task: Task,
) -> None:
    """Without this, a task that runs out of attempts would sit in 'failed'
    forever, and 'failed' suppresses a fresh planner proposal for the same work
    unconditionally. Only 'abandoned' is exempt from that. This is the fix."""
    output = {"succeeded": False, "summary": "Nope.", "failure_reason": "still broken"}
    result_msg = _result_message(structured_output=output, model_usage=PLANNER_USAGE)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query(result_msg),
    )

    for _attempt in range(app_config.limits.max_attempts):
        session.refresh(task)
        if task.state == TaskState.FAILED:
            task.transition_to(TaskState.QUEUED)
            session.commit()
        await runner.run_worker(
            session,
            agent_name=AgentName.MARKETER,
            task=task,
            company_profile=_profile(),
            user_prompt="Draft something.",
        )

    session.refresh(task)
    assert task.attempts == app_config.limits.max_attempts
    assert task.state == TaskState.ABANDONED
    assert task.result["attempts"] == app_config.limits.max_attempts
    assert "still broken" in task.result["abandoned_reason"]


async def test_exceeding_the_per_task_cost_ceiling_abandons_before_attempts_run_out(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
    task: Task,
) -> None:
    """max_attempts bounds retries, not spend. A task that costs more than
    max_usd_per_task must be abandoned even on its very first attempt, with
    plenty of attempts still nominally available.

    max_usd_per_run is kept comfortably above the marketer's real fixed
    prompt cost so the pre-dispatch input-size check (which measures the
    actual system prompt, task prompt and schema before ever calling the
    fake query_fn) lets the call through; it is the *actual* reported cost
    below, not the estimate, that then blows through max_usd_per_task.
    """
    tight = app_config.model_copy(
        update={
            "budget": app_config.budget.model_copy(
                update={
                    "max_usd_per_run": 0.01,
                    "max_usd_per_task": 0.01,
                    "max_usd_per_day": 1.0,
                    "max_usd_per_company": 10.0,
                }
            )
        }
    )
    guard = BudgetGuard(tight)
    output = {"succeeded": False, "summary": "Nope.", "failure_reason": "too expensive"}
    # Costs $0.05, well over the $0.01 task ceiling, on a single attempt.
    expensive_usage = {
        "claude-sonnet-5": {"inputTokens": 500, "outputTokens": 100, "costUSD": 0.05}
    }
    result_msg = _result_message(structured_output=output, model_usage=expensive_usage)
    runner = AgentRunner(
        app_config=tight,
        budget_guard=guard,
        adapter_registry=registry,
        query_fn=_fake_query(result_msg),
    )

    assert tight.limits.max_attempts > 1  # plenty of attempts nominally left

    await runner.run_worker(
        session,
        agent_name=AgentName.MARKETER,
        task=task,
        company_profile=_profile(),
        user_prompt="Draft something.",
    )

    session.refresh(task)
    assert task.attempts == 1
    assert task.attempts < tight.limits.max_attempts
    assert task.state == TaskState.ABANDONED
    assert "cost" in task.result["abandoned_because"]


# ------------------------------------------------------ mid-run budget watchdog


async def test_an_interrupted_run_is_abandoned_not_requeued_at_identical_scope(
    session,
    app_config: AppConfig,
    registry: AdapterRegistry,
    company: Company,
    task: Task,
) -> None:
    """The leak this closes: a task that is genuinely too big for one run was
    coming back as ``failed`` and getting requeued at the exact same scope, up to
    two more times, burning budget each time before max_attempts finally caught
    up with it. On its very first attempt, with attempts and task budget both
    nowhere near exhausted, a mid-stream cutoff must abandon it outright.

    max_tokens_per_run is set above the marketer's own fixed-prompt estimate
    (~1,037 tokens for this profile) so the pre-dispatch check passes and the
    fake stream's usage is what trips the mid-stream watchdog instead — this
    test is specifically about the INTERRUPTED path, not BUDGET_BLOCKED."""
    tight = app_config.model_copy(
        update={"budget": app_config.budget.model_copy(update={"max_tokens_per_run": 1_500})}
    )
    guard = BudgetGuard(tight)

    async def fake(*, prompt: str, options: object):
        yield AssistantMessage(
            content=[TextBlock(text="reading...")],
            model="claude-sonnet-5",
            usage={"input_tokens": 2_000, "output_tokens": 0},
        )

    runner = AgentRunner(
        app_config=tight,
        budget_guard=guard,
        adapter_registry=registry,
        query_fn=fake,
    )

    assert task.attempts == 0
    await runner.run_worker(
        session,
        agent_name=AgentName.MARKETER,
        task=task,
        company_profile=_profile(),
        user_prompt="Draft something.",
    )

    session.refresh(task)
    assert task.attempts == 1  # nowhere near max_attempts
    assert task.state == TaskState.ABANDONED
    assert task.result["needs_rescoping"] is True
    assert "run ceiling" in task.result["abandoned_because"]


async def test_a_cutoff_run_recovers_a_valid_answer_the_model_already_gave(
    session,
    app_config: AppConfig,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    """The bug this closes, found live: a real run's watchdog fired on the exact
    same message that carried a fully valid AgentResult for a task that had, in
    fact, already finished, and the task was wrongly abandoned for it. The Run
    row must still honestly say INTERRUPTED (the SDK call really was cut off,
    and the halt still stands), but the task itself must not be thrown away."""
    tight = app_config.model_copy(
        update={
            "budget": app_config.budget.model_copy(update={"max_tokens_per_run": 3_000}),
            "agents": {
                **app_config.agents,
                AgentName.PLANNER: app_config.agents[AgentName.PLANNER].model_copy(
                    update={"max_usd_per_run": None, "max_tokens_per_run": None}
                ),
            },
        }
    )
    guard = BudgetGuard(tight)
    valid_answer = '{"tasks": [], "no_action_reason": "Nothing new since last cycle."}'

    async def fake(*, prompt: str, options: object):
        yield AssistantMessage(
            content=[
                ToolUseBlock(id="tu_1", name="StructuredOutput", input={"input": valid_answer})
            ],
            model="claude-sonnet-5",
            usage={"input_tokens": 6_000, "output_tokens": 0},
        )

    runner = AgentRunner(
        app_config=tight,
        budget_guard=guard,
        adapter_registry=registry,
        query_fn=fake,
    )

    outcome = await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert outcome.run.status == RunStatus.INTERRUPTED
    assert "Recovered a valid structured output" in outcome.run.error
    assert outcome.output is not None
    assert outcome.output.is_empty

    # The halt from the genuine overshoot still stands: recovering the answer
    # is not a reason to also relax the budget control that caught it.
    halt = session.execute(
        select(BudgetHalt).where(BudgetHalt.limit_name == "mid_run_watchdog")
    ).scalar_one()
    assert halt.scope == BudgetScope.RUN


async def test_a_run_is_actively_cut_off_when_it_crosses_its_own_token_ceiling(
    session,
    app_config: AppConfig,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    """The bug this closes: a run that reserved 200k tokens once spent 2.03M,
    because nothing checked usage until the run had already finished. This proves
    the watchdog fires mid-stream, not after: a third message that would push the
    run further over is never even pulled from the stream.

    The planner's own per-agent override is cleared here so this test exercises
    the global figure specifically, regardless of what default.yaml ships for the
    planner (see the separate per-agent-override test for that)."""
    tight = app_config.model_copy(
        update={
            "budget": app_config.budget.model_copy(update={"max_tokens_per_run": 3_000}),
            "agents": {
                **app_config.agents,
                AgentName.PLANNER: app_config.agents[AgentName.PLANNER].model_copy(
                    update={"max_usd_per_run": None, "max_tokens_per_run": None}
                ),
            },
        }
    )
    guard = BudgetGuard(tight)
    pulled: list[str] = []

    async def fake(*, prompt: str, options: object):
        for label, tokens in [("first", 1_500), ("second", 1_800), ("third", 30)]:
            pulled.append(label)
            yield AssistantMessage(
                content=[TextBlock(text=label)],
                model="claude-sonnet-5",
                usage={"input_tokens": tokens, "output_tokens": 0},
            )

    runner = AgentRunner(
        app_config=tight,
        budget_guard=guard,
        adapter_registry=registry,
        query_fn=fake,
    )

    outcome = await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert outcome.output is None
    assert outcome.run.status == RunStatus.INTERRUPTED
    assert "ceiling" in outcome.run.error
    # The generator was closed right after "second" pushed the total over 3,000;
    # "third" must never have been pulled at all.
    assert pulled == ["first", "second"]
    assert outcome.run.input_tokens == 3_300

    halt = session.execute(
        select(BudgetHalt).where(BudgetHalt.limit_name == "mid_run_watchdog")
    ).scalar_one()
    assert halt.scope == BudgetScope.RUN
    assert halt.company_id == company.id


async def test_a_per_agent_ceiling_override_is_what_the_watchdog_actually_checks(
    session,
    app_config: AppConfig,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    """The global max_tokens_per_run is left untouched and is nowhere near being
    crossed by this sequence. Only the planner's own override (1,000) is tight
    enough to cut it off, proving the watchdog reads the per-agent figure rather
    than always falling back to the global default."""
    overridden = app_config.model_copy(
        update={
            "agents": {
                **app_config.agents,
                AgentName.PLANNER: app_config.agents[AgentName.PLANNER].model_copy(
                    update={"max_tokens_per_run": 3_000}
                ),
            }
        }
    )
    assert overridden.budget.max_tokens_per_run > 3_000  # the global figure is untouched
    guard = BudgetGuard(overridden)
    pulled: list[str] = []

    async def fake(*, prompt: str, options: object):
        for label, tokens in [("first", 1_500), ("second", 1_800), ("third", 30)]:
            pulled.append(label)
            yield AssistantMessage(
                content=[TextBlock(text=label)],
                model="claude-sonnet-5",
                usage={"input_tokens": tokens, "output_tokens": 0},
            )

    runner = AgentRunner(
        app_config=overridden,
        budget_guard=guard,
        adapter_registry=registry,
        query_fn=fake,
    )

    outcome = await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert outcome.run.status == RunStatus.INTERRUPTED
    assert pulled == ["first", "second"]


async def test_the_watchdog_does_not_fire_on_a_run_that_stays_at_or_under_its_ceiling(
    session,
    app_config: AppConfig,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    """A guard that fires when it shouldn't is as bad as one that doesn't fire at
    all. Accumulated usage lands exactly ON the token ceiling (not over it) after
    the second message, deliberately testing the boundary rather than a
    comfortably-clear case, then the run finishes normally: every message must
    still be pulled, the run must succeed, and no watchdog halt is written.

    The planner's own per-agent override is cleared here so this test exercises
    the global figure specifically, regardless of what default.yaml ships for the
    planner."""
    tight = app_config.model_copy(
        update={
            "budget": app_config.budget.model_copy(update={"max_tokens_per_run": 3_000}),
            "agents": {
                **app_config.agents,
                AgentName.PLANNER: app_config.agents[AgentName.PLANNER].model_copy(
                    update={"max_usd_per_run": None, "max_tokens_per_run": None}
                ),
            },
        }
    )
    guard = BudgetGuard(tight)
    pulled: list[str] = []
    output = {"tasks": [], "no_action_reason": "Nothing new since last cycle."}
    result_msg = _result_message(structured_output=output, model_usage=PLANNER_USAGE)

    async def fake(*, prompt: str, options: object):
        for label, tokens in [("first", 1_500), ("second", 1_500)]:
            pulled.append(label)
            yield AssistantMessage(
                content=[TextBlock(text=label)],
                model="claude-sonnet-5",
                usage={"input_tokens": tokens, "output_tokens": 0},
            )
        pulled.append("result")
        yield result_msg

    runner = AgentRunner(
        app_config=tight,
        budget_guard=guard,
        adapter_registry=registry,
        query_fn=fake,
    )

    outcome = await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert pulled == ["first", "second", "result"]
    assert outcome.run.status == RunStatus.SUCCEEDED
    assert outcome.output is not None

    halt_count = (
        session.execute(select(BudgetHalt).where(BudgetHalt.limit_name == "mid_run_watchdog"))
        .scalars()
        .all()
    )
    assert halt_count == []


async def test_the_pre_dispatch_check_refuses_without_ever_calling_the_sdk(
    session,
    app_config: AppConfig,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    """Input size alone, known before dispatch, must be enough to refuse a call.
    The fake query_fn here raises if it is ever invoked, so a passing test proves
    the SDK was never reached.

    The planner's own per-agent override is cleared here so this test exercises
    the global figure specifically, regardless of what default.yaml ships for the
    planner."""
    tight = app_config.model_copy(
        update={
            "budget": app_config.budget.model_copy(update={"max_usd_per_run": 0.0001}),
            "agents": {
                **app_config.agents,
                AgentName.PLANNER: app_config.agents[AgentName.PLANNER].model_copy(
                    update={"max_usd_per_run": None, "max_tokens_per_run": None}
                ),
            },
        }
    )
    guard = BudgetGuard(tight)

    async def fake_that_must_not_be_called(*, prompt: str, options: object):
        raise AssertionError("The SDK must not be called once the pre-dispatch check refuses.")
        yield  # pragma: no cover - unreachable, makes this an async generator

    runner = AgentRunner(
        app_config=tight,
        budget_guard=guard,
        adapter_registry=registry,
        query_fn=fake_that_must_not_be_called,
    )

    outcome = await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert outcome.output is None
    assert outcome.run.status == RunStatus.BUDGET_BLOCKED
    assert "Refused rather than dispatched" in outcome.run.error
    assert outcome.run.cost_usd == 0.0

    halt = session.execute(
        select(BudgetHalt).where(BudgetHalt.limit_name == "pre_dispatch_input_size")
    ).scalar_one()
    assert halt.scope == BudgetScope.RUN
    assert halt.observed_value > tight.budget.max_usd_per_run


# ----------------------------------------------------- planner retry on invalid output


def _sequenced_query(*sequences: list[object]):
    """A ``query_fn`` that replays one message sequence per call, in order, and
    records the prompt each call received. A call beyond the last sequence raises
    IndexError, so a runaway retry loop fails the test instead of passing quietly."""
    prompts: list[str] = []
    remaining = list(sequences)

    async def fake(*, prompt: str, options: object) -> AsyncIterator[object]:
        prompts.append(prompt)
        for message in remaining.pop(0):
            yield message

    fake.prompts = prompts  # type: ignore[attr-defined]
    return fake


# No no_action_reason: the exact shape of the reply rejected live on 2 Oct 2026.
_REJECTED_PLAN = {"assessment": "Remaining budget is small.", "tasks": []}
_ACCEPTED_PLAN = {"tasks": [], "no_action_reason": "Nothing worth doing this cycle."}


def _planner_runner(app_config, budget_guard, registry, query):
    return AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=query,
    )


async def test_a_planner_reply_that_fails_validation_is_retried_with_the_reason(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    query = _sequenced_query(
        [_result_message(structured_output=_REJECTED_PLAN, model_usage=PLANNER_USAGE)],
        [_result_message(structured_output=_ACCEPTED_PLAN, model_usage=PLANNER_USAGE)],
    )
    runner = _planner_runner(app_config, budget_guard, registry, query)

    outcome = await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert outcome.run.status == RunStatus.SUCCEEDED
    assert outcome.output is not None
    assert outcome.output.is_empty
    assert query.prompts[0] == "What next?"
    assert query.prompts[1].startswith("What next?")
    assert "rejected by validation" in query.prompts[1]
    assert "no_action_reason" in query.prompts[1]
    statuses = [r.status for r in session.execute(select(Run).order_by(Run.id)).scalars()]
    assert statuses == [RunStatus.INVALID_OUTPUT, RunStatus.SUCCEEDED]


async def test_a_planner_that_fails_validation_twice_gives_up_after_one_retry(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    query = _sequenced_query(
        [_result_message(structured_output=_REJECTED_PLAN, model_usage=PLANNER_USAGE)],
        [_result_message(structured_output=_REJECTED_PLAN, model_usage=PLANNER_USAGE)],
    )
    runner = _planner_runner(app_config, budget_guard, registry, query)

    outcome = await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert outcome.output is None
    assert outcome.run.status == RunStatus.INVALID_OUTPUT
    assert len(query.prompts) == 2


async def test_a_valid_planner_reply_is_not_retried(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
) -> None:
    query = _sequenced_query(
        [_result_message(structured_output=_ACCEPTED_PLAN, model_usage=PLANNER_USAGE)],
    )
    runner = _planner_runner(app_config, budget_guard, registry, query)

    await runner.run_planner(
        session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
    )

    assert len(query.prompts) == 1


# ------------------------------------------- a failed launch must not leave a run "running"


@pytest.mark.parametrize(
    "exc",
    [
        ProcessError(
            "Command failed with exit code 127", exit_code=127, stderr="setpriv: setresuid failed"
        ),
        RuntimeError("something nobody anticipated"),
    ],
    ids=["process-error", "unexpected"],
)
async def test_an_exception_from_the_sdk_call_closes_the_run_row_instead_of_leaving_it_running(
    session,
    app_config: AppConfig,
    budget_guard: BudgetGuard,
    registry: AdapterRegistry,
    company: Company,
    exc: Exception,
) -> None:
    """3 to 6 Oct 2026: the agent wrapper exited 127 (it could not switch user), the SDK
    raised ProcessError, and nothing closed the run row written as ``running`` before
    the call. Four planner rows showed in progress for days, and the activity feed
    showed nothing had gone wrong."""
    from polska.db.enums import ActivityKind
    from polska.db.models.activity import ActivityEvent

    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=registry,
        query_fn=_fake_query_raising(exc),
    )

    with pytest.raises(type(exc)):
        await runner.run_planner(
            session, company_id=company.id, company_profile=_profile(), user_prompt="What next?"
        )

    # The caller's session is discarded when the exception propagates out of the tick, so
    # anything only flushed (not committed) is lost. Read back what actually survived.
    session.rollback()
    rows = session.execute(select(Run)).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == RunStatus.FAILED
    assert rows[0].finished_at is not None
    assert type(exc).__name__ in rows[0].error
    errors = session.execute(
        select(ActivityEvent).where(ActivityEvent.kind == ActivityKind.ERROR)
    ).scalars()
    assert [e.run_id for e in errors] == [rows[0].id]
