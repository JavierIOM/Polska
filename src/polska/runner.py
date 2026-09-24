"""The agent runner: one call to the Claude Agent SDK, fully accounted for.

Every run goes through :meth:`AgentRunner._invoke`. It reserves budget before the
call, builds the SDK options from config (model, tool allowlist, the agent's system
prompt, a JSON schema the CLI enforces on the way out), executes, and writes a
:class:`Run` row whatever happened: success, a schema violation, a timeout, or the
CLI reporting its own failure. Nothing here ever assigns to ``Task.state`` directly;
:meth:`run_worker` calls ``Task.transition_to`` exactly as the state machine requires.

Output is never string-parsed. ``output_format`` passes the Pydantic schema's JSON
Schema straight to the CLI's ``--json-schema``, which constrains the model's final
answer; the result comes back on ``ResultMessage.structured_output`` and is validated
against the same schema again on this end before anything downstream reads a field
of it. Two checks, because a CLI flag being honoured is not the same guarantee as a
Pydantic model actually validating it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, TypeVar

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    Message,
    ResultError,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk import (
    query as sdk_query,
)
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from polska.activity import log
from polska.adapters.registry import AdapterRegistry
from polska.budget import BudgetExceeded, BudgetGuard
from polska.config.appconfig import AppConfig
from polska.config.company import CompanyProfile
from polska.db.enums import ActivityKind, AgentName, RunStatus, TaskState
from polska.db.models import Run, Task
from polska.db.types import utcnow
from polska.gate import dispatch_action
from polska.prompts import build_system_prompt
from polska.schemas.actions import AgentResult

logger = logging.getLogger("polska.runner")

SchemaT = TypeVar("SchemaT", bound=BaseModel)

#: The SDK-native async generator signature ``AgentRunner`` calls. Tests substitute a
#: fake with the same signature so nothing in this module ever reaches the network.
QueryFn = Callable[..., AsyncIterator[Message]]


class InvocationResult:
    """What one call to the SDK produced, whatever it produced.

    Deliberately not just "the validated schema or None": a failed run still has a
    ``Run`` row worth returning to the caller, and callers need to branch on
    ``output is None`` far more often than they need the raw messages.
    """

    __slots__ = ("output", "run")

    def __init__(self, output: BaseModel | None, run: Run) -> None:
        self.output = output
        self.run = run


def _extract_usage(
    result_message: ResultMessage | None, app_config: AppConfig, model: str
) -> tuple[int, int, int, int, float]:
    """(input, output, cache_read, cache_creation, cost_usd) for one run.

    Prefers the SDK's own per-model usage (``model_usage``), which is Anthropic's own
    figure for what this call actually cost, summed across entries in case of a
    fallback model switch mid-run. Falls back to the flatter ``usage`` dict plus this
    project's own pricing table only when the SDK reported no per-model breakdown at
    all, which happens when a run ends before any usage was ever reported.
    """
    if result_message is None:
        return 0, 0, 0, 0, 0.0

    if result_message.model_usage:
        entries = list(result_message.model_usage.values())
        return (
            sum(int(e.get("inputTokens", 0)) for e in entries),
            sum(int(e.get("outputTokens", 0)) for e in entries),
            sum(int(e.get("cacheReadInputTokens", 0)) for e in entries),
            sum(int(e.get("cacheCreationInputTokens", 0)) for e in entries),
            sum(float(e.get("costUSD", 0.0)) for e in entries),
        )

    if result_message.usage:
        usage = result_message.usage
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        cache_read = int(usage.get("cache_read_input_tokens", 0))
        cache_creation = int(usage.get("cache_creation_input_tokens", 0))
        if result_message.total_cost_usd is not None:
            cost_usd = result_message.total_cost_usd
        else:
            price = app_config.price_for(model)
            cost_usd = price.cost_usd(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
            )
        return input_tokens, output_tokens, cache_read, cache_creation, cost_usd

    return 0, 0, 0, 0, 0.0


class AgentRunner:
    """Executes one agent invocation at a time, fully accounted for.

    Concurrency across multiple invocations (how many run at once, which task goes
    next) belongs to the orchestrator, in phase 3. This class only knows how to run
    one thing to completion, correctly.
    """

    def __init__(
        self,
        *,
        app_config: AppConfig,
        budget_guard: BudgetGuard,
        adapter_registry: AdapterRegistry,
        query_fn: QueryFn = sdk_query,
    ) -> None:
        self._config = app_config
        self._budget = budget_guard
        self._adapters = adapter_registry
        self._query_fn = query_fn

    async def run_planner(
        self,
        session: Session,
        *,
        company_id: int,
        company_profile: CompanyProfile,
        user_prompt: str,
    ) -> InvocationResult:
        """One planner cycle. No task, no tools, no gate: it only proposes."""
        from polska.schemas.planner import PlannerOutput

        return await self._invoke(
            session,
            agent_name=AgentName.PLANNER,
            company_id=company_id,
            task=None,
            company_profile=company_profile,
            user_prompt=user_prompt,
            schema_model=PlannerOutput,
            cwd=None,
        )

    async def run_dedup_judge(
        self,
        session: Session,
        *,
        company_id: int,
        user_prompt: str,
    ) -> InvocationResult:
        """One dedup judge call over an ambiguous batch. No task, no tools."""
        from polska.schemas.planner import DedupJudgeOutput

        return await self._invoke(
            session,
            agent_name=AgentName.DEDUP_JUDGE,
            company_id=company_id,
            task=None,
            company_profile=None,
            user_prompt=user_prompt,
            schema_model=DedupJudgeOutput,
            cwd=None,
        )

    async def run_worker(
        self,
        session: Session,
        *,
        agent_name: AgentName,
        task: Task,
        company_profile: CompanyProfile,
        user_prompt: str,
        workspace: Path | None = None,
    ) -> InvocationResult:
        """Run a worker agent (engineer, marketer, support, analyst) against a task.

        Owns the task's transition out of ``queued``: it moves to ``running`` before
        the call and to exactly one of ``done``, ``awaiting_approval`` or ``failed``
        after, depending on what the agent returned and what the gate did with any
        actions it proposed. The caller decides *whether* to run this task; this
        method is what actually running it means.
        """
        task.transition_to(TaskState.RUNNING)
        session.commit()

        outcome = await self._invoke(
            session,
            agent_name=agent_name,
            company_id=task.company_id,
            task=task,
            company_profile=company_profile,
            user_prompt=user_prompt,
            schema_model=AgentResult,
            cwd=workspace,
        )

        result = outcome.output
        if not isinstance(result, AgentResult):
            task.transition_to(
                TaskState.FAILED,
                error=outcome.run.error or "The agent's output did not validate.",
            )
            session.commit()
            return outcome

        if not result.succeeded:
            task.transition_to(TaskState.FAILED, error=result.failure_reason)
            session.commit()
            return outcome

        any_pending = False
        for action in result.actions:
            approval = await dispatch_action(
                session,
                self._config,
                self._adapters,
                company_id=task.company_id,
                task_id=task.id,
                run_id=outcome.run.id,
                action=action,
            )
            if approval is not None and approval.is_pending:
                any_pending = True

        task_result = {"summary": result.summary, "output": result.output}
        if any_pending:
            task.transition_to(TaskState.AWAITING_APPROVAL, result=task_result)
        else:
            task.transition_to(TaskState.DONE, result=task_result)
        session.commit()
        return outcome

    async def _invoke(
        self,
        session: Session,
        *,
        agent_name: AgentName,
        company_id: int,
        task: Task | None,
        company_profile: CompanyProfile | None,
        user_prompt: str,
        schema_model: type[SchemaT],
        cwd: Path | None,
    ) -> InvocationResult:
        agent_config = self._config.agents[agent_name]
        model = agent_config.model

        try:
            reservation = await self._budget.reserve(session, company_id=company_id, model=model)
        except BudgetExceeded as exc:
            # Recorded as a Run, the same as any other outcome, rather than left as
            # a bare exception: "every ceiling crossing is recorded" means a row
            # exists to show it, not just a log line that scrolled past.
            run = self._build_run_row(
                session,
                agent_name=agent_name,
                company_id=company_id,
                task=task,
                model=model,
                system_prompt="",
                user_prompt=user_prompt,
                raw_output=None,
                tools_called=[],
                usage=(0, 0, 0, 0, 0.0),
                status=RunStatus.BUDGET_BLOCKED,
                error=str(exc),
                started=utcnow(),
                session_id=None,
                duration_ms=0,
            )
            session.commit()
            log(
                session,
                company_id=company_id,
                kind=ActivityKind.BUDGET_HALT,
                summary=f"{agent_name.value} blocked before it started: {exc.halt.limit_name}",
                task_id=task.id if task else None,
                run_id=run.id,
                error=str(exc),
            )
            return InvocationResult(output=None, run=run)

        started = utcnow()
        log(
            session,
            company_id=company_id,
            kind=ActivityKind.RUN_STARTED,
            summary=f"{agent_name.value} started on {model}",
            task_id=task.id if task else None,
        )
        try:
            run, output = await self._execute_and_record(
                session,
                agent_name=agent_name,
                company_id=company_id,
                task=task,
                company_profile=company_profile,
                user_prompt=user_prompt,
                schema_model=schema_model,
                cwd=cwd,
                agent_config=agent_config,
                model=model,
                started=started,
            )
        finally:
            self._budget.release(reservation)

        overshoot = self._budget.check_run_did_not_overshoot(
            session, company_id=company_id, run=run
        )
        if overshoot is not None:
            log(
                session,
                company_id=company_id,
                kind=ActivityKind.BUDGET_HALT,
                summary=f"Run {run.id} exceeded its own token reservation",
                task_id=task.id if task else None,
                run_id=run.id,
                approval_id=None,
                error=overshoot.reason,
            )

        log(
            session,
            company_id=company_id,
            kind=ActivityKind.RUN_FINISHED,
            summary=f"{agent_name.value} finished: {run.status.value}",
            task_id=task.id if task else None,
            run_id=run.id,
            error=run.error,
        )
        return InvocationResult(output=output, run=run)

    async def _execute_and_record(
        self,
        session: Session,
        *,
        agent_name: AgentName,
        company_id: int,
        task: Task | None,
        company_profile: CompanyProfile | None,
        user_prompt: str,
        schema_model: type[BaseModel],
        cwd: Path | None,
        agent_config: Any,
        model: str,
        started: Any,
    ) -> tuple[Run, BaseModel | None]:
        system_prompt = build_system_prompt(agent_name, agent_config, company=company_profile)
        schema = schema_model.model_json_schema()
        max_run_tokens = self._config.budget.max_tokens_per_run
        max_budget_usd = self._config.price_for(model).cost_usd(
            input_tokens=0, output_tokens=max_run_tokens
        )

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=list(agent_config.tools),
            # The tool allowlist is the security boundary, chosen deliberately per
            # agent. There is nobody present to answer an interactive permission
            # prompt in a scheduler that fires unattended, so prompting is not an
            # option here; bypassPermissions is what running unattended means.
            permission_mode="bypassPermissions",
            model=model,
            max_turns=agent_config.max_turns,
            cwd=str(cwd) if cwd is not None else None,
            output_format={"type": "json_schema", "schema": schema},
            max_budget_usd=max_budget_usd,
        )

        tools_called: list[dict[str, Any]] = []
        text_parts: list[str] = []
        result_message: ResultMessage | None = None
        status = RunStatus.RUNNING
        error_text: str | None = None
        raw_output: str | None = None
        structured: Any = None
        validated: BaseModel | None = None

        try:
            async with asyncio.timeout(agent_config.timeout_seconds):
                async for message in self._query_fn(prompt=user_prompt, options=options):
                    self._collect_message(message, tools_called, text_parts)
                    if isinstance(message, ResultMessage):
                        result_message = message
        except TimeoutError:
            status = RunStatus.TIMED_OUT
            error_text = f"Exceeded its {agent_config.timeout_seconds}s wall-clock timeout."
        except ResultError as exc:
            status = RunStatus.FAILED
            error_text = str(exc)
            if result_message is None:
                # The CLI never yielded a ResultMessage before raising; take what we
                # can from the exception itself so the failure is still legible.
                result_message = None
                raw_output = exc.result
        except (CLIConnectionError, CLINotFoundError):
            # This is not "this task failed", it is "the CLI is not usable at all".
            # Record what happened so it is not silent, then let it propagate: a
            # broken install should stop the caller, not be swallowed per-task.
            status = RunStatus.FAILED
            error_text = "The Claude Agent SDK's CLI could not be reached or found."
            run = self._build_run_row(
                session,
                agent_name=agent_name,
                company_id=company_id,
                task=task,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                raw_output=None,
                tools_called=tools_called,
                usage=(0, 0, 0, 0, 0.0),
                status=status,
                error=error_text,
                started=started,
                session_id=None,
                duration_ms=None,
            )
            session.commit()
            raise

        if result_message is not None:
            raw_output = raw_output or result_message.result
            if status == RunStatus.RUNNING:
                if result_message.is_error:
                    status = RunStatus.FAILED
                    error_text = result_message.result or "The CLI reported is_error."
                else:
                    structured = result_message.structured_output

        if raw_output is None and text_parts:
            raw_output = "".join(text_parts)

        if status == RunStatus.RUNNING:
            try:
                if structured is None:
                    raise ValueError(
                        "No structured_output was returned despite an output_format "
                        "schema being set on the request."
                    )
                # Validated here, against the same Pydantic model whose JSON Schema
                # was handed to the CLI. The CLI honouring --json-schema is not the
                # same guarantee as this actually passing, so both checks run.
                validated = schema_model.model_validate(structured)
                status = RunStatus.SUCCEEDED
            except (ValidationError, ValueError) as exc:
                status = RunStatus.INVALID_OUTPUT
                error_text = str(exc)
                validated = None

        usage = _extract_usage(result_message, self._config, model)
        run = self._build_run_row(
            session,
            agent_name=agent_name,
            company_id=company_id,
            task=task,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            raw_output=raw_output,
            tools_called=tools_called,
            usage=usage,
            status=status,
            error=error_text,
            started=started,
            session_id=result_message.session_id if result_message else None,
            duration_ms=result_message.duration_ms if result_message else None,
        )
        session.commit()
        return run, validated

    @staticmethod
    def _collect_message(
        message: Message,
        tools_called: list[dict[str, Any]],
        text_parts: list[str],
    ) -> None:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tools_called.append(
                        {"id": block.id, "name": block.name, "input": block.input, "is_error": None}
                    )
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    for call in tools_called:
                        if call["id"] == block.tool_use_id:
                            call["is_error"] = bool(block.is_error)

    def _build_run_row(
        self,
        session: Session,
        *,
        agent_name: AgentName,
        company_id: int,
        task: Task | None,
        model: str,
        system_prompt: str,
        user_prompt: str,
        raw_output: str | None,
        tools_called: list[dict[str, Any]],
        usage: tuple[int, int, int, int, float],
        status: RunStatus,
        error: str | None,
        started: Any,
        session_id: str | None,
        duration_ms: int | None,
    ) -> Run:
        input_tokens, output_tokens, cache_read, cache_creation, cost_usd = usage
        fx_rate = self._config.budget.usd_to_gbp
        finished = utcnow()
        run = Run(
            company_id=company_id,
            task_id=task.id if task else None,
            agent=agent_name,
            model=model,
            session_id=session_id,
            system_prompt=system_prompt,
            prompt=user_prompt,
            raw_output=raw_output,
            tools_called=tools_called,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            cost_usd=cost_usd,
            cost_gbp=cost_usd * fx_rate,
            fx_rate=fx_rate,
            status=status,
            error=error,
            duration_ms=duration_ms
            if duration_ms is not None
            else int((finished - started).total_seconds() * 1000),
            started_at=started,
            finished_at=finished,
        )
        session.add(run)
        session.flush()
        return run
