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
import json
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
from polska.budget import BudgetExceeded, BudgetGuard, actual_usd_for_task, write_halt
from polska.config.appconfig import AppConfig
from polska.config.company import CompanyProfile
from polska.db.enums import ActivityKind, AgentName, BudgetScope, RunStatus, TaskState
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

#: Conservative chars-per-token for the pre-dispatch size estimate below: deliberately
#: lower than the ~4 chars/token an English-prose rule of thumb would give, so this
#: estimate errs toward refusing early rather than under-counting and letting an
#: oversized prompt through. There is no token-counting call here (the Messages API's
#: count_tokens would be exact, but it is a network round trip on the critical path of
#: deciding whether to spend money at all, and this project's pricing is already in
#: dollars-per-token, not something that needs perfect precision to be a useful gate).
_CONSERVATIVE_CHARS_PER_TOKEN = 3.0


def _estimate_tokens(text: str) -> int:
    """A deliberately pessimistic token estimate for text known before dispatch."""
    return int(len(text) / _CONSERVATIVE_CHARS_PER_TOKEN)


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


#: The CLI's own internal tool name for handing back --json-schema output. Not
#: exposed as an importable constant anywhere in the SDK; confirmed by directly
#: inspecting tools_called on a real run whose stream was cut off right after
#: this exact tool call.
_STRUCTURED_OUTPUT_TOOL_NAME = "StructuredOutput"


def _recover_structured_output(
    tools_called: list[dict[str, Any]], schema_model: type[BaseModel]
) -> tuple[Any, BaseModel] | None:
    """The model's own last valid answer, if the stream was cut off before the
    CLI could turn it into a proper ``ResultMessage``.

    This is the fix for a real incident: the mid-stream watchdog closed the
    stream on the exact same message that carried a fully valid ``AgentResult``
    for a task that had, in fact, finished -- and nothing was looking at
    ``tools_called`` to notice, so a real, paid-for answer was thrown away and
    the task wrongly abandoned. Scans backwards so a run that called the output
    tool more than once (the model retrying its own earlier, invalid attempt)
    recovers the LAST one, matching what ``ResultMessage.structured_output``
    would have held had the stream been allowed to finish naturally.
    """
    for call in reversed(tools_called):
        if call.get("name") != _STRUCTURED_OUTPUT_TOOL_NAME:
            continue
        raw = call.get("input", {}).get("input")
        if not isinstance(raw, str):
            continue
        try:
            structured = json.loads(raw)
            validated = schema_model.model_validate(structured)
        except (ValueError, ValidationError):
            continue
        return structured, validated
    return None


class _RunningUsage:
    """Usage accumulated live, one ``AssistantMessage`` at a time.

    ``AssistantMessage.usage`` is per-turn, not cumulative (confirmed against the
    SDK's own source), so this adds each turn in rather than taking the latest value.
    This is what lets a run be judged against its ceiling *before* it ends, which is
    the entire point: the post-hoc check in budget.py only ever sees the final total.
    """

    __slots__ = ("cache_creation", "cache_read", "input_tokens", "output_tokens")

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_creation = 0

    def add(self, usage: dict[str, Any]) -> None:
        self.input_tokens += int(usage.get("input_tokens", 0))
        self.output_tokens += int(usage.get("output_tokens", 0))
        self.cache_read += int(usage.get("cache_read_input_tokens", 0))
        self.cache_creation += int(usage.get("cache_creation_input_tokens", 0))

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_creation

    def cost_usd(self, app_config: AppConfig, model: str) -> float:
        price = app_config.price_for(model)
        return price.cost_usd(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read,
            cache_creation_tokens=self.cache_creation,
        )

    def as_tuple(self, app_config: AppConfig, model: str) -> tuple[int, int, int, int, float]:
        return (
            self.input_tokens,
            self.output_tokens,
            self.cache_read,
            self.cache_creation,
            self.cost_usd(app_config, model),
        )

    def exceeds(
        self, app_config: AppConfig, model: str, *, max_tokens_per_run: int, max_usd_per_run: float
    ) -> str | None:
        """A human-readable reason the run has crossed its own ceiling, or ``None``.

        ``max_tokens_per_run``/``max_usd_per_run`` are the caller's already-resolved
        figures for this specific agent (see ``AppConfig.max_usd_per_run_for``), not
        the bare global default: an engineer and a planner do not share a ceiling.
        """
        tokens = self.total_tokens
        if tokens > max_tokens_per_run:
            return (
                f"Cut off mid-stream: {tokens} tokens spent against a "
                f"{max_tokens_per_run} token run ceiling."
            )
        cost = self.cost_usd(app_config, model)
        if cost > max_usd_per_run:
            return (
                f"Cut off mid-stream: ${cost:.4f} spent against a "
                f"${max_usd_per_run:.2f} run ceiling."
            )
        return None


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
        agent_cli_path: str | None = None,
    ) -> None:
        self._config = app_config
        self._budget = budget_guard
        self._adapters = adapter_registry
        self._query_fn = query_fn
        # Every agent invocation this instance makes is pointed at this path
        # instead of the SDK's own CLI discovery, when set. See
        # Settings.resolved_agent_cli_wrapper_path for what verifies it
        # actually exists before this ever gets here.
        self._agent_cli_path = agent_cli_path

    @property
    def budget_guard(self) -> BudgetGuard:
        """The one instance every dispatch reserves against. Exposed so the
        planner prompt can quote its actual remaining-budget figure instead of
        a separately derived one -- see orchestrator._build_planner_prompt."""
        return self._budget

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
        the call and to one of ``done``, ``awaiting_approval``, ``failed``,
        ``abandoned`` or ``blocked`` after, depending on what the agent returned,
        what the gate did with any actions it proposed, and whether this was the
        task's last permitted attempt (see :meth:`fail_or_abandon`). ``blocked``
        is distinct from the other outcomes: it means the agent reported that this
        environment cannot execute the task at all, not that it tried and failed
        (see :meth:`_block_as_not_executable`). The caller decides *whether* to run
        this task; this method is what actually running it means.
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
            self.fail_or_abandon(
                session,
                task,
                outcome.run.error or "The agent's output did not validate.",
                run_status=outcome.run.status,
            )
            return outcome

        if result.is_environment_blocked:
            self._block_as_not_executable(session, task, result.blocked_reason)
            return outcome

        if not result.succeeded:
            self.fail_or_abandon(
                session, task, result.failure_reason, run_status=outcome.run.status
            )
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

    def _block_as_not_executable(self, session: Session, task: Task, reason: str) -> None:
        """Move a task straight to ``blocked``: the agent reported that this
        environment cannot execute it at all, not that it tried and failed.

        Never routed through :meth:`fail_or_abandon`, and never subject to
        ``limits.max_attempts`` or ``budget.max_usd_per_task``: those exist to
        decide whether *another attempt* is worth paying for, which only makes
        sense for a task that could plausibly succeed on a retry. An
        environment gap does not go away because the same task is dispatched
        again at the same scope; a human fixing the gap (or deciding it can't
        be fixed) is the only thing that changes the answer. This is exactly
        the case ``run_worker``'s docstring means by "reports rather than
        improvises" -- the fix for the failure mode this closes is not a
        retry policy, it is the agent choosing to stop and say so.
        """
        task.transition_to(
            TaskState.BLOCKED,
            result={"blocked_reason": reason, "attempts": task.attempts},
        )
        summary = f"Blocked, not executable in this environment: {reason}"
        session.commit()
        log(
            session,
            company_id=task.company_id,
            kind=ActivityKind.TASK_STATE_CHANGED,
            summary=summary,
            task_id=task.id,
        )

    def fail_or_abandon(
        self,
        session: Session,
        task: Task,
        reason: str,
        *,
        run_status: RunStatus | None = None,
    ) -> None:
        """Move a task out of ``running`` after a failure, to ``failed`` if it may
        still be retried, or to ``abandoned`` if it has run out of either attempts
        or budget, or was cut off for crossing its own run ceiling.

        Without this, a task that exhausts ``limits.max_attempts`` would sit in
        ``failed`` forever, and ``failed`` suppresses a fresh planner proposal for
        the same work unconditionally, by design, on the reasoning that it is the
        orchestrator's own retry queue. Once there is no more retrying to do, that
        reasoning no longer holds: the task must become ``abandoned``, the one state
        the dedup rules never suppress, or the underlying need goes quiet with
        nothing left that will ever try it again.

        Three independent reasons can exhaust a task, checked separately because
        they measure different things:

        - ``limits.max_attempts`` bounds retries.
        - ``budget.max_usd_per_task`` bounds cumulative spend. A task can hit the
          cost ceiling with attempts still available, on an expensive model, and it
          must not be allowed one more attempt just because the attempt counter
          has room left.
        - ``run_status is RunStatus.INTERRUPTED``: the mid-stream watchdog cut this
          run off for crossing its own ceiling while it ran, on its very first
          attempt if that is when it happened. That is not a substantive failure
          worth retrying unchanged: the task itself is too big for one run, and
          requeuing it at identical scope would just burn the same ceiling again,
          attempt after attempt, until ``max_attempts`` finally caught up with it.
          Deliberately narrower than ``RunStatus.BUDGET_BLOCKED`` here: a blocked
          run can mean this task's own input was too big (a real "too big" signal,
          same as INTERRUPTED) or it can mean the *company* is halted for a reason
          that has nothing to do with this task's size at all (an unrelated open
          halt, a day or company ceiling) — conflating the two would abandon a
          perfectly reasonable task for someone else's overshoot. Only the
          unambiguous case is handled automatically; the rest stays ``failed`` and
          the ordinary retry/backoff path decides what happens next.

        ``failed`` itself is not requeued here: whether and when to retry a failed
        task is a scheduling decision (capacity, backoff) that belongs to the
        orchestrator's loop, not to one invocation of the runner.
        """
        attempts_exhausted = task.attempts >= self._config.limits.max_attempts
        spent = actual_usd_for_task(session, task.id)
        budget_exhausted = spent >= self._config.budget.max_usd_per_task
        ceiling_exceeded = run_status is RunStatus.INTERRUPTED

        if attempts_exhausted or budget_exhausted or ceiling_exceeded:
            if ceiling_exceeded:
                why = "exceeded its own run ceiling; needs narrower scope before retrying"
            elif attempts_exhausted:
                why = f"attempts ({task.attempts}/{self._config.limits.max_attempts})"
            else:
                why = f"cost (${spent:.4f}/${self._config.budget.max_usd_per_task:.2f})"
            task.transition_to(
                TaskState.ABANDONED,
                result={
                    "abandoned_reason": reason,
                    "abandoned_because": why,
                    "needs_rescoping": ceiling_exceeded,
                    "attempts": task.attempts,
                    "max_attempts": self._config.limits.max_attempts,
                    "spent_usd": spent,
                    "max_usd_per_task": self._config.budget.max_usd_per_task,
                },
            )
            summary = f"Abandoned, exhausted {why}: {reason}"
            kind = ActivityKind.TASK_STATE_CHANGED
        else:
            task.transition_to(TaskState.FAILED, error=reason)
            summary = (
                f"Failed (attempt {task.attempts}/{self._config.limits.max_attempts}, "
                f"${spent:.4f}/${self._config.budget.max_usd_per_task:.2f} spent): {reason}"
            )
            kind = ActivityKind.TASK_STATE_CHANGED
        session.commit()
        log(
            session,
            company_id=task.company_id,
            kind=kind,
            summary=summary,
            task_id=task.id,
            error=reason,
        )

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
            reservation = await self._budget.reserve(
                session, company_id=company_id, model=model, agent_name=agent_name
            )
        except BudgetExceeded as exc:
            # Recorded as a Run, the same as any other outcome, rather than left as
            # a bare exception: "every ceiling crossing is recorded" means a row
            # exists to show it, not just a log line that scrolled past. Written
            # directly as budget_blocked, never as running: the SDK was never
            # called, so there is no async gap for a crash to land in here.
            run = Run(
                company_id=company_id,
                task_id=task.id if task else None,
                agent=agent_name,
                model=model,
                prompt=user_prompt,
                tools_called=[],
                status=RunStatus.BUDGET_BLOCKED,
                error=str(exc),
                started_at=utcnow(),
                finished_at=utcnow(),
                duration_ms=0,
            )
            session.add(run)
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
        for halt in overshoot:
            log(
                session,
                company_id=company_id,
                kind=ActivityKind.BUDGET_HALT,
                summary=f"Run {run.id} exceeded its own {halt.limit_name} reservation",
                task_id=task.id if task else None,
                run_id=run.id,
                approval_id=None,
                error=halt.reason,
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
        # Resolved once, here: this agent's own override if it has one, else the
        # global default. Every check below (pre-dispatch, the SDK's own
        # max_budget_usd, the mid-stream watchdog, the halt it writes) uses these
        # same two numbers, so none of them can disagree about which ceiling
        # applies to this run.
        max_usd_per_run = self._config.max_usd_per_run_for(agent_name)
        max_tokens_per_run = self._config.max_tokens_per_run_for(agent_name)

        # Measured before a single token is sent. This cannot see what a worker's
        # own tool calls will pull in later (that is what the mid-stream watchdog
        # below is for), but the fixed part of the request -- the system prompt,
        # the task prompt, the schema handed to --json-schema -- is fully known
        # right now, and there is no reason to ever dispatch a call whose input
        # alone already costs more than the run is allowed to spend.
        blocked = self._check_input_size(
            session,
            agent_name=agent_name,
            company_id=company_id,
            task=task,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            started=started,
            max_usd_per_run=max_usd_per_run,
            max_tokens_per_run=max_tokens_per_run,
        )
        if blocked is not None:
            return blocked, None

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            # `tools` is what actually restricts which tools exist at all; the CLI
            # only receives `--tools` when this is set to something other than
            # None (confirmed in the SDK's own subprocess_cli.py). `allowed_tools`
            # only controls auto-approval of an interactive permission prompt,
            # which bypassPermissions below already makes moot regardless -- it
            # was, on its own, never a restriction on availability. Found live,
            # 30 Sep 2026: this file had only ever set allowed_tools since phase 2
            # (24 Sep), so every agent has had the CLI's full default toolset
            # (WebSearch, WebFetch, Task, NotebookEdit, ...) rather than its
            # configured list. Both are set now, deliberately: allowed_tools stays
            # harmless to keep even though bypassPermissions moots it, in case
            # permission_mode is ever loosened later for some agent.
            tools=list(agent_config.tools),
            allowed_tools=list(agent_config.tools),
            # There is nobody present to answer an interactive permission prompt
            # in a scheduler that fires unattended, so prompting is not an option
            # here; bypassPermissions is what running unattended means. `tools`
            # above is what actually enforces the boundary this comment used to
            # claim allowed_tools was providing.
            permission_mode="bypassPermissions",
            model=model,
            max_turns=agent_config.max_turns,
            cwd=str(cwd) if cwd is not None else None,
            # Every agent invocation, unconditionally, not just the ones with
            # Bash or WebFetch in their tool allowlist: one boundary regardless
            # of which agent runs is simpler to reason about and audit than
            # "only the ones that could plausibly reach the network" ever was.
            # None outside the container (see Settings.agent_cli_wrapper_path),
            # which leaves the SDK's own CLI discovery, unrestricted, exactly
            # as before this existed.
            cli_path=self._agent_cli_path,
            output_format={"type": "json_schema", "schema": schema},
            # The SDK's own enforcement of the same figure the reservation holds.
            # Real, but not exact: measured directly, a run has still spent 5% over
            # this figure before the SDK's own check caught it. The mid-stream
            # watchdog below is what actually holds the line; this is one more
            # layer, not the layer.
            max_budget_usd=max_usd_per_run,
            # SDK isolation mode. Left at its default (None), every call loads
            # ~/.claude/settings.json, any .claude/settings.json or
            # .claude/settings.local.json found from cwd, and CLAUDE.md: whoever's
            # personal Claude Code configuration happens to be on the host,
            # completely unrelated to running a company. Measured directly: this
            # was inflating a single dedup judge call to 28k+ cached tokens before
            # this was set. An empty list is the SDK's own name for "load nothing
            # from disk"; it is not related to `tools` above, which governs which
            # tools the agent may call regardless of this setting.
            setting_sources=[],
            effort=agent_config.effort,
        )

        # Written before the SDK is ever called, in `running` state, so a crash
        # between here and the call completing leaves a real row behind rather than
        # a silent gap: that row is exactly what reconcile_orphaned_runs recovers at
        # the next process start. Everything below updates this same row in place.
        run = self._create_running_run(
            session,
            agent_name=agent_name,
            company_id=company_id,
            task=task,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            started=started,
        )

        tools_called: list[dict[str, Any]] = []
        text_parts: list[str] = []
        result_message: ResultMessage | None = None
        status = RunStatus.RUNNING
        error_text: str | None = None
        raw_output: str | None = None
        structured: Any = None
        validated: BaseModel | None = None
        # Accumulated as messages stream in, so a run that never reaches a
        # ResultMessage (timed out, errored, or actively cut off below) still has
        # a real usage figure recorded instead of the zero _extract_usage(None, ...)
        # would otherwise give it.
        running = _RunningUsage()

        try:
            async with asyncio.timeout(agent_config.timeout_seconds):
                stream = self._query_fn(prompt=user_prompt, options=options)
                async for message in stream:
                    self._collect_message(message, tools_called, text_parts)
                    if isinstance(message, ResultMessage):
                        result_message = message
                    elif isinstance(message, AssistantMessage) and message.usage:
                        running.add(message.usage)
                        overshoot = running.exceeds(
                            self._config,
                            model,
                            max_tokens_per_run=max_tokens_per_run,
                            max_usd_per_run=max_usd_per_run,
                        )
                        if overshoot is not None:
                            # This is the fix for a real incident: a run spent
                            # 10x its reservation because nothing checked usage
                            # until the run had already finished (or the SDK's
                            # own max_budget_usd happened to catch it, which
                            # measured 5% over before it did). Stopping the
                            # stream here is what makes the ceiling a control
                            # rather than a number recorded after the fact.
                            status = RunStatus.INTERRUPTED
                            error_text = overshoot
                            await stream.aclose()
                            break
        except TimeoutError:
            status = RunStatus.TIMED_OUT
            error_text = f"Exceeded its {agent_config.timeout_seconds}s wall-clock timeout."
        except ResultError as exc:
            status = RunStatus.FAILED
            error_text = str(exc)
            if result_message is None:
                # The CLI never yielded a ResultMessage before raising; take what we
                # can from the exception itself so the failure is still legible.
                raw_output = exc.result
        except (CLIConnectionError, CLINotFoundError):
            # This is not "this task failed", it is "the CLI is not usable at all".
            # The row already exists in `running`; close it out so it does not sit
            # there looking orphaned, then let the exception propagate: a broken
            # install should stop the caller, not be swallowed per-task.
            self._finalize_run(
                session,
                run=run,
                raw_output=None,
                tools_called=tools_called,
                usage=running.as_tuple(self._config, model),
                status=RunStatus.FAILED,
                error="The Claude Agent SDK's CLI could not be reached or found.",
                session_id=None,
                duration_ms=None,
                started=started,
            )
            raise

        if status == RunStatus.INTERRUPTED:
            self._write_run_halt(
                session,
                company_id=company_id,
                run=run,
                observed_value=running.cost_usd(self._config, model),
                limit_value=max_usd_per_run,
                limit_name="mid_run_watchdog",
                reason=error_text or "Run interrupted after crossing its own ceiling mid-stream.",
            )
            log(
                session,
                company_id=company_id,
                kind=ActivityKind.BUDGET_HALT,
                summary=f"{agent_name.value} cut off mid-stream: crossed its own run ceiling",
                task_id=task.id if task else None,
                run_id=run.id,
                error=error_text,
            )

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

        if result_message is None and status in (RunStatus.INTERRUPTED, RunStatus.TIMED_OUT):
            # The Run row still honestly records that the SDK call itself was cut
            # off (status stays INTERRUPTED/TIMED_OUT, the halt above still stands):
            # this only changes whether the TASK is wrongly abandoned for work it
            # had, in fact, already finished. run_worker decides the task's fate
            # from `validated`, not from `run.status`, so setting this is what
            # lets a recovered task proceed as done instead.
            recovered = _recover_structured_output(tools_called, schema_model)
            if recovered is not None:
                structured, validated = recovered
                error_text = (
                    f"{error_text} Recovered a valid structured output from before "
                    "the cutoff; the task is not abandoned for this."
                )

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

        if result_message is not None:
            usage = _extract_usage(result_message, self._config, model)
        else:
            usage = running.as_tuple(self._config, model)

        self._finalize_run(
            session,
            run=run,
            raw_output=raw_output,
            tools_called=tools_called,
            usage=usage,
            status=status,
            error=error_text,
            session_id=result_message.session_id if result_message else None,
            duration_ms=result_message.duration_ms if result_message else None,
            started=started,
        )
        return run, validated

    def _check_input_size(
        self,
        session: Session,
        *,
        agent_name: AgentName,
        company_id: int,
        task: Task | None,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        started: Any,
        max_usd_per_run: float,
        max_tokens_per_run: int,
    ) -> Run | None:
        """Refuse to dispatch if the *known* input alone would already breach the
        run's ceiling. Returns a written, terminal ``Run`` row if it does, else
        ``None`` to mean "fine, carry on". ``max_usd_per_run``/``max_tokens_per_run``
        are this agent's already-resolved figures, not the bare global default."""
        estimated_tokens = (
            _estimate_tokens(system_prompt)
            + _estimate_tokens(user_prompt)
            + _estimate_tokens(json.dumps(schema))
        )
        price = self._config.price_for(model)
        estimated_usd = price.cost_usd(input_tokens=estimated_tokens, output_tokens=0)

        within_tokens = estimated_tokens <= max_tokens_per_run
        within_usd = estimated_usd <= max_usd_per_run
        if within_tokens and within_usd:
            return None

        reason = (
            f"Input alone is an estimated {estimated_tokens} tokens (~${estimated_usd:.4f} "
            f"at {model}'s input rate) before a single reply token: over the "
            f"{max_tokens_per_run} token / ${max_usd_per_run:.2f} run ceiling "
            "on the known, fixed part of the request alone. Refused rather than dispatched."
        )
        run = Run(
            company_id=company_id,
            task_id=task.id if task else None,
            agent=agent_name,
            model=model,
            system_prompt=system_prompt,
            prompt=user_prompt,
            tools_called=[],
            status=RunStatus.BUDGET_BLOCKED,
            error=reason,
            started_at=started,
            finished_at=utcnow(),
            duration_ms=0,
        )
        session.add(run)
        session.flush()
        self._write_run_halt(
            session,
            company_id=company_id,
            run=run,
            observed_value=estimated_usd,
            limit_value=max_usd_per_run,
            limit_name="pre_dispatch_input_size",
            reason=reason,
        )
        log(
            session,
            company_id=company_id,
            kind=ActivityKind.BUDGET_HALT,
            summary=(
                f"{agent_name.value} refused before dispatch: input alone exceeds its run ceiling"
            ),
            task_id=task.id if task else None,
            run_id=run.id,
            error=reason,
        )
        return run

    def _write_run_halt(
        self,
        session: Session,
        *,
        company_id: int,
        run: Run,
        observed_value: float,
        limit_value: float,
        limit_name: str,
        reason: str,
    ) -> None:
        write_halt(
            session,
            company_id=company_id,
            run_id=run.id,
            scope=BudgetScope.RUN,
            limit_name=limit_name,
            limit_value=limit_value,
            observed_value=observed_value,
            period_key=None,
            reason=f"Run {run.id}: {reason}",
        )

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

    def _create_running_run(
        self,
        session: Session,
        *,
        agent_name: AgentName,
        company_id: int,
        task: Task | None,
        model: str,
        system_prompt: str,
        user_prompt: str,
        started: Any,
    ) -> Run:
        """Write the row before the SDK is ever called. See the module docstring
        and ``reconcile_orphaned_runs`` for why: a crash after this point leaves a
        real row behind, not a silent gap in the ledger."""
        run = Run(
            company_id=company_id,
            task_id=task.id if task else None,
            agent=agent_name,
            model=model,
            system_prompt=system_prompt,
            prompt=user_prompt,
            tools_called=[],
            status=RunStatus.RUNNING,
            started_at=started,
        )
        session.add(run)
        session.commit()
        return run

    def _finalize_run(
        self,
        session: Session,
        *,
        run: Run,
        raw_output: str | None,
        tools_called: list[dict[str, Any]],
        usage: tuple[int, int, int, int, float],
        status: RunStatus,
        error: str | None,
        session_id: str | None,
        duration_ms: int | None,
        started: Any,
    ) -> Run:
        """Update the row :meth:`_create_running_run` wrote, in place, with what
        actually happened. Never creates a second row: one dispatch is one row,
        whatever its outcome."""
        input_tokens, output_tokens, cache_read, cache_creation, cost_usd = usage
        fx_rate = self._config.budget.usd_to_gbp
        finished = utcnow()

        run.session_id = session_id
        run.raw_output = raw_output
        run.tools_called = tools_called
        run.input_tokens = input_tokens
        run.output_tokens = output_tokens
        run.cache_read_tokens = cache_read
        run.cache_creation_tokens = cache_creation
        run.cost_usd = cost_usd
        run.cost_gbp = cost_usd * fx_rate
        run.fx_rate = fx_rate
        run.status = status
        run.error = error
        run.duration_ms = (
            duration_ms
            if duration_ms is not None
            else int((finished - started).total_seconds() * 1000)
        )
        run.finished_at = finished
        session.commit()
        return run
