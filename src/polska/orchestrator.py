"""The orchestrator loop: one company's tick, from planning to dispatch.

Each tick: sync the company's profile, ask the planner what to do, deduplicate its
proposals, enqueue what survives, requeue any ``FAILED`` task whose backoff has
elapsed, then dispatch as many ``QUEUED`` tasks as capacity allows.

Two different sessions are deliberately in play. Planning (sync, plan, dedup,
enqueue, requeue) is inherently sequential and runs on one session for the whole
phase. Dispatch runs tasks *concurrently*, up to ``limits.max_concurrent_tasks``, and
a single synchronous ``Session`` is not safe to share across concurrent callers: each
dispatched task gets its own session, opened fresh and closed when that task's run
finishes. The shared ``AgentRunner`` (and the ``BudgetGuard`` inside it) is safe to
reuse across those concurrent calls, since its own state is guarded by an
``asyncio.Lock``, but a `Session` is not, so it is never shared that way.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from polska.activity import log
from polska.budget import active_halt, reconcile_orphaned_runs
from polska.config.appconfig import AppConfig, LimitsConfig
from polska.config.company import (
    CompanyProfile,
    LoadedProfile,
    discover_profiles,
    load_company_profile,
)
from polska.db.enums import ActivityKind, AgentName, GoalStatus, RunStatus, TaskState, TaskType
from polska.db.models import ActivityEvent, Goal, Run, Task
from polska.db.state import ACTIVE_STATES
from polska.db.types import utcday, utcnow
from polska.dedup import deduplicate
from polska.gate import resolve_waiting_tasks
from polska.runner import AgentRunner
from polska.schemas.planner import PlannerOutput, ProposedTask
from polska.sync import reconcile_removed_companies, sync_company
from polska.workspace import (
    WorkspaceError,
    prepare_task_workspace,
    reclaim_node_modules_for_terminal_tasks,
)

logger = logging.getLogger("polska.orchestrator")

#: Which worker agent handles which task type. Fixed and total: every TaskType maps
#: to exactly one agent, so a task can never be silently unroutable.
TASK_TYPE_TO_AGENT: dict[TaskType, AgentName] = {
    TaskType.ENGINEERING: AgentName.ENGINEER,
    TaskType.MARKETING: AgentName.MARKETER,
    TaskType.SUPPORT: AgentName.SUPPORT,
    TaskType.RESEARCH: AgentName.ANALYST,
}

#: States a "recent outcomes" query looks at: work that has actually concluded.
_CONCLUDED_STATES = frozenset(
    {TaskState.DONE, TaskState.FAILED, TaskState.ABANDONED, TaskState.BLOCKED}
)

#: Task types that get a repo clone in their workspace. Research needs to read
#: real code as much as engineering does, arguably more: every worker mapped to
#: TaskType.RESEARCH (the analyst) only ever reads, so giving it the clone carries
#: none of the write-access risk engineering's own tool allowlist otherwise would.
_TASK_TYPES_WITH_A_WORKSPACE = frozenset({TaskType.ENGINEERING, TaskType.RESEARCH})


class TickSummary:
    """What happened in one company's tick. Returned for logging and for tests to
    assert against, rather than having to re-derive it from the activity feed."""

    __slots__ = (
        "company_id",
        "skipped_inactive",
        "halted_by",
        "planner_status",
        "planner_error",
        "proposed",
        "deduped_out",
        "skipped_daily_cap",
        "enqueued",
        "requeued",
        "dispatched",
    )

    def __init__(self, company_id: int) -> None:
        self.company_id = company_id
        self.skipped_inactive = False
        #: Set when an open budget halt stopped this tick before it planned anything.
        self.halted_by: str | None = None
        #: How the planner's own run ended. Without this, "proposed=0" cannot tell a
        #: planner that chose to do nothing from one whose output was rejected.
        self.planner_status: RunStatus | None = None
        self.planner_error: str | None = None
        self.proposed = 0
        self.deduped_out = 0
        self.skipped_daily_cap = 0
        self.enqueued = 0
        self.requeued = 0
        self.dispatched = 0


def retry_delay_seconds(attempts: int, limits: LimitsConfig) -> float:
    """Wall-clock seconds a failed task must wait before it may retry.

    ``min(base * multiplier ** (attempts - 1), max_delay)``. ``attempts`` is the
    count already used, so the first retry (attempts == 1) waits exactly the base
    delay, and each one after doubles (with the default multiplier) up to the cap.
    """
    exponent = max(0, attempts - 1)
    delay = limits.retry_base_delay_seconds * (limits.retry_backoff_multiplier**exponent)
    return min(delay, limits.retry_max_delay_seconds)


def is_retry_eligible(task: Task, limits: LimitsConfig, now: dt.datetime) -> bool:
    """True if a ``FAILED`` task's backoff has elapsed and it may be requeued.

    Measured from ``Task.updated_at`` (set automatically by ``transition_to``), not
    a count of scheduler ticks: this needs no tick-counter of its own, and it
    degrades sensibly if ``scheduler.interval_hours`` changes later.
    """
    if task.state != TaskState.FAILED:
        return False
    elapsed = (now - task.updated_at).total_seconds()
    return elapsed >= retry_delay_seconds(task.attempts, limits)


#: Structured, per-task-safe reason for a concluded task's outcome, keyed by its
#: most recent Run's status. Deliberately never the raw exception text a Run or
#: Task.error might carry: that text can (and, in the incident this closes, did)
#: describe a completely different run's cost figures -- a company-wide halt or
#: another task's own ceiling -- which the planner then mined as if it were
#: "the" budget figure. Nothing here ever contains a number.
_RUN_STATUS_REASON: dict[RunStatus, str] = {
    RunStatus.SUCCEEDED: "succeeded",
    RunStatus.FAILED: "the agent reported failure",
    RunStatus.TIMED_OUT: "timed out",
    RunStatus.INTERRUPTED: "was cut off for exceeding its own run ceiling",
    RunStatus.BUDGET_BLOCKED: (
        "was blocked before it started by a budget ceiling or an open halt "
        "(not necessarily one this task itself caused)"
    ),
    RunStatus.ORPHANED: "was interrupted by a process restart",
    RunStatus.RECONCILED: "was interrupted by a process restart",
    RunStatus.INVALID_OUTPUT: "produced output that did not validate",
}


def _dry_run_action_types(session: Session, task_id: int) -> list[str]:
    """Action types this task had executed only by the dry-run adapter (chosen, or
    substituted by ``integrations.force_dry_run``). Read from the activity feed, which
    records the adapter that actually ran for every execution, approved or direct."""
    events = session.execute(
        select(ActivityEvent).where(
            ActivityEvent.task_id == task_id, ActivityEvent.kind == ActivityKind.ACTION_EXECUTED
        )
    ).scalars()
    return sorted(
        {
            (event.detail or {}).get("action_type", "action")
            for event in events
            if (event.detail or {}).get("adapter") == "dry_run"
        }
    )


def _task_outcome_reason(session: Session, task: Task) -> str:
    """A structured, number-free description of why a concluded task ended the
    way it did. See ``_RUN_STATUS_REASON`` for why this replaces raw error text.
    """
    if task.state == TaskState.DONE:
        summary = (task.result or {}).get("summary") or ""
        not_applied = _dry_run_action_types(session, task.id)
        if not_applied:
            # First, so the 300-character line limit can never cut it off. Found 7 Oct
            # 2026: the planner treated a dry-run "commit and push" as landed and set the
            # next task to mirror a change that was never in the repository.
            return (
                f"NOT APPLIED: its {', '.join(not_applied)} was approved but only recorded "
                f"by the dry-run adapter, so whatever it changed does not exist. {summary}"
            )[:300]
        return summary[:300]

    if task.state == TaskState.ABANDONED:
        # Already structured and specific to this task (see fail_or_abandon):
        # "exceeded its own run ceiling", "attempts (n/m)", "cost ($/$ of this
        # task's own ceiling)". Its dollar figure, when present, is this task's
        # own spend against its own per-task ceiling, never another run's.
        return (task.result or {}).get("abandoned_because") or "abandoned"

    if task.state == TaskState.BLOCKED:
        # The agent's own stated reason the environment could not run this at
        # all, e.g. "no TypeScript runtime available to verify the change".
        # Distinct from a failure: retrying will not change this, only fixing
        # the environment (or deciding not to) will, and this line exists so
        # the planner stops proposing the same doomed work rather than
        # treating it as ordinary flaky work worth another attempt.
        reason = (task.result or {}).get("blocked_reason") or "not executable here"
        return f"blocked, not executable in this environment: {reason}"

    last_run = session.execute(
        select(Run).where(Run.task_id == task.id).order_by(Run.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    if last_run is None:
        return "failed before any run was attempted"
    return _RUN_STATUS_REASON.get(last_run.status, "failed")


def _build_planner_prompt(
    session: Session, company_id: int, open_goals: list[Goal], runner: AgentRunner
) -> str:
    lines = ["Open goals:"]
    if not open_goals:
        lines.append("  (none open)")
    for goal in open_goals:
        lines.append(
            f"  - [{goal.key}] {goal.title}: {goal.current_value:g}/{goal.target_value:g} "
            f"{goal.unit} ({goal.progress:.0%}), priority {goal.priority}"
        )

    recent = list(
        session.execute(
            select(Task)
            .where(Task.company_id == company_id, Task.state.in_(_CONCLUDED_STATES))
            .order_by(Task.updated_at.desc())
            .limit(10)
        ).scalars()
    )

    lines.append("")
    lines.append("Recent outcomes, most recent first:")
    for task in recent:
        outcome = _task_outcome_reason(session, task)
        lines.append(f"  - [{task.state.value}] {task.title}: {outcome}"[:300])
    if not recent:
        lines.append("  (none yet)")

    # AgentResult.observations: things a worker noticed outside its own task, which
    # exist precisely so the next plan can act on them.
    noticed = [
        f"  - (task {task.id}) {str(note)[:200]}"
        for task in recent
        if task.state == TaskState.DONE
        for note in ((task.result or {}).get("observations") or [])[:3]
    ][:6]
    if noticed:
        lines.append("")
        lines.append("Things recent workers noticed outside their own task, most recent first:")
        lines.extend(noticed)

    active_count = session.execute(
        select(func.count(Task.id)).where(
            Task.company_id == company_id, Task.state.in_(ACTIVE_STATES)
        )
    ).scalar_one()
    queued_count = session.execute(
        select(func.count(Task.id)).where(
            Task.company_id == company_id, Task.state == TaskState.QUEUED
        )
    ).scalar_one()
    lines.append("")
    lines.append(
        f"Currently in flight: {active_count}. Queued, not yet dispatched: {queued_count}."
    )

    remaining_today = runner.budget_guard.remaining_today_usd(session, company_id)
    lines.append("")
    lines.append(
        f"Remaining budget for today, the one figure to use for this: "
        f"${remaining_today:.2f} (of the ${runner.budget_guard.budget.max_usd_per_day:.2f} "
        "daily ceiling, after today's committed spend and anything already in flight)."
    )
    return "\n".join(lines)


def _build_worker_prompt(task: Task, goal: Goal | None) -> str:
    parts = [f"Task: {task.title}", "", task.description or "(no further description given)"]
    if task.rationale:
        parts.append(f"\nWhy this task exists: {task.rationale}")
    if goal is not None:
        # The goal's own description, not just its title and numbers: an agent's
        # workspace is a read-only repo clone, which has no way to see the
        # company profile a goal is defined in. Found live: an analyst task
        # burned real tokens hunting the repo for another goal's definition,
        # which structurally cannot exist there -- it lives in this project's
        # own database and companies/*.yaml, invisible from inside the company's
        # own repo. Any agent working toward a goal needs that goal's text
        # handed to it here; it has no other way to ever see it.
        parts.append(
            f"\nThis serves the goal '{goal.title}' "
            f"(currently {goal.current_value:g}/{goal.target_value:g} {goal.unit}):"
            f"\n{goal.description or '(no further description given)'}"
        )
    return "\n".join(parts)


async def run_company_tick(
    session_factory: sessionmaker[Session],
    app_config: AppConfig,
    runner: AgentRunner,
    loaded_profile: LoadedProfile,
    workspace_root: Path,
) -> TickSummary:
    """One company's full cycle: plan, dedup, enqueue, requeue, dispatch."""
    profile = loaded_profile.profile
    dispatch_ids: list[int] = []

    with session_factory() as session:
        company = sync_company(session, loaded_profile)
        summary = TickSummary(company_id=company.id)

        if not company.active:
            summary.skipped_inactive = True
            log(
                session,
                company_id=company.id,
                kind=ActivityKind.SCHEDULER_TICK,
                summary=f"{company.slug} is inactive; tick skipped.",
            )
            session.commit()
            return summary

        halt = active_halt(session, company.id)
        if halt is not None:
            # An open halt already refuses every reservation. Planning and dispatching
            # through it anyway only records a blocked run per agent, and charges each
            # queued task an attempt for a run that never happens: three ticks of that
            # and the task is abandoned without ever having run.
            summary.halted_by = f"halt {halt.id} ({halt.limit_name})"
            log(
                session,
                company_id=company.id,
                kind=ActivityKind.BUDGET_HALT,
                summary=(
                    f"Tick skipped: {company.slug} is stopped by {summary.halted_by}. "
                    "Clear it on the dashboard's budget page to resume."
                ),
            )
            session.commit()
            return summary

        log(
            session,
            company_id=company.id,
            kind=ActivityKind.SCHEDULER_TICK,
            summary=f"Tick started for {company.slug}.",
        )

        open_goals = list(
            session.execute(
                select(Goal)
                .where(Goal.company_id == company.id, Goal.status == GoalStatus.ACTIVE)
                .order_by(Goal.priority)
            ).scalars()
        )

        log(
            session,
            company_id=company.id,
            kind=ActivityKind.PLAN_STARTED,
            summary="Planning cycle started.",
        )
        planner_prompt = _build_planner_prompt(session, company.id, open_goals, runner)
        plan_outcome = await runner.run_planner(
            session, company_id=company.id, company_profile=profile, user_prompt=planner_prompt
        )
        plan = plan_outcome.output
        summary.planner_status = plan_outcome.run.status
        summary.planner_error = plan_outcome.run.error

        if isinstance(plan, PlannerOutput):
            if plan.is_empty:
                log(
                    session,
                    company_id=company.id,
                    kind=ActivityKind.PLAN_COMPLETED,
                    summary=f"No action proposed: {plan.no_action_reason}",
                )
            else:
                await _enqueue_proposals(session, app_config, runner, company.id, plan, summary)
                log(
                    session,
                    company_id=company.id,
                    kind=ActivityKind.PLAN_COMPLETED,
                    summary=(
                        f"Plan applied: {summary.proposed} proposed, {summary.enqueued} enqueued, "
                        f"{summary.deduped_out} deduped out, {summary.skipped_daily_cap} skipped "
                        "for the daily cap."
                    ),
                )
        else:
            log(
                session,
                company_id=company.id,
                kind=ActivityKind.PLAN_COMPLETED,
                summary="Planner run did not produce a usable plan; see its Run row.",
                error=plan_outcome.run.error,
            )

        _requeue_eligible_failures(session, app_config.limits, company.id, summary)

        active_count = session.execute(
            select(func.count(Task.id)).where(
                Task.company_id == company.id, Task.state.in_(ACTIVE_STATES)
            )
        ).scalar_one()
        capacity = max(0, app_config.limits.max_concurrent_tasks - active_count)

        if capacity:
            to_dispatch = list(
                session.execute(
                    select(Task)
                    .where(Task.company_id == company.id, Task.state == TaskState.QUEUED)
                    .order_by(Task.priority, Task.created_at)
                    .limit(capacity)
                ).scalars()
            )
            dispatch_ids = [task.id for task in to_dispatch]

    if dispatch_ids:
        results = await asyncio.gather(
            *(
                _dispatch_one(session_factory, runner, profile, workspace_root, task_id)
                for task_id in dispatch_ids
            ),
            return_exceptions=True,
        )
        fatal: BaseException | None = None
        for result in results:
            if isinstance(result, BaseException):
                # Every other dispatch this tick has already been accounted for;
                # only now is it safe to surface a fatal, non-per-task exception
                # (e.g. the CLI itself being unreachable) rather than lose track
                # of the ones that succeeded alongside it.
                fatal = result
            else:
                summary.dispatched += 1
        if fatal is not None:
            raise fatal

    return summary


class UnknownCompanyError(LookupError):
    """A cycle was asked for one company by slug and no profile has that name."""


@dataclass
class CycleResult:
    #: Slug to what its tick did, for every company whose tick completed.
    summaries: dict[str, TickSummary] = field(default_factory=dict)
    #: Companies whose profile would not load or whose tick raised.
    failed: list[str] = field(default_factory=list)

    @property
    def planner_problems(self) -> list[str]:
        """Slugs whose planner run did not succeed."""
        return [
            slug
            for slug, summary in self.summaries.items()
            if summary.planner_status not in (None, RunStatus.SUCCEEDED)
        ]

    @property
    def halted(self) -> list[str]:
        """Slugs an open budget halt stopped before anything was planned."""
        return [slug for slug, summary in self.summaries.items() if summary.halted_by]


def _reconcile_removed(session_factory: sessionmaker[Session], companies_dir: Path) -> None:
    try:
        with session_factory() as session:
            abandoned = reconcile_removed_companies(session, companies_dir)
    except Exception:
        # Housekeeping must never be what stops a company from being planned for.
        logger.exception("Reconciling removed companies failed; continuing with the tick.")
        return
    if abandoned:
        logger.warning(
            "Reconciled %d task(s) whose company's profile is no longer loaded.", abandoned
        )


def _settle_approvals(session_factory: sessionmaker[Session]) -> None:
    try:
        with session_factory() as session:
            settled = resolve_waiting_tasks(session)
    except Exception:
        logger.exception("Settling tasks awaiting approval failed; continuing with the tick.")
        return
    if settled:
        logger.info("Moved %d task(s) out of awaiting_approval.", settled)


def _reclaim_node_modules(
    session_factory: sessionmaker[Session], app_config: AppConfig, workspace_root: Path
) -> None:
    try:
        with session_factory() as session:
            reclaimed = reclaim_node_modules_for_terminal_tasks(
                session,
                workspace_root,
                grace_period=dt.timedelta(
                    hours=app_config.limits.workspace_node_modules_grace_hours
                ),
            )
    except Exception:
        logger.exception("Reclaiming node_modules failed; the tick itself already ran.")
        return
    if reclaimed:
        logger.info(
            "Reclaimed node_modules for %d workspace(s) past their grace period.", reclaimed
        )


async def run_tick_cycle(
    session_factory: sessionmaker[Session],
    app_config: AppConfig,
    runner: AgentRunner,
    companies_dir: Path,
    workspace_root: Path,
    *,
    only_slug: str | None = None,
) -> CycleResult:
    """Everything one firing of the schedule does, for every company (or just
    ``only_slug``). The scheduler job and ``polska-cli tick`` both call this, so
    a manual tick exercises the same code as the scheduled one.

    Profiles are reloaded from disk every time, so an edited or newly added company
    YAML is picked up without a restart. Housekeeping brackets the company ticks
    and is isolated from them: reconcile first (so a removed company's tasks are not
    dispatched), reclaim last, and a failure in either is logged and skipped.
    Before this, an exception in cleanup ran ahead of every company and cancelled the
    whole day's planning.

    Raises :class:`UnknownCompanyError` before doing anything else if ``only_slug``
    matches no profile.
    """
    paths = list(discover_profiles(companies_dir))
    if only_slug is not None:
        paths = [p for p in paths if p.stem == only_slug]
        if not paths:
            raise UnknownCompanyError(only_slug)

    _reconcile_removed(session_factory, companies_dir)
    # Before the company ticks: a task still parked awaiting approval holds one of the
    # max_concurrent_tasks slots, so settling them first frees capacity for this tick.
    _settle_approvals(session_factory)

    result = CycleResult()
    for path in paths:
        try:
            loaded = load_company_profile(path)
        except Exception:
            # A malformed profile must not take every other company's tick down
            # with it.
            logger.exception("Failed to load company profile %s; skipped this tick.", path)
            result.failed.append(path.stem)
            continue

        slug = loaded.profile.slug
        try:
            summary = await run_company_tick(
                session_factory, app_config, runner, loaded, workspace_root
            )
        except Exception:
            # One company's fatal error (e.g. the CLI itself being unreachable,
            # which run_company_tick deliberately re-raises) must not stop the
            # scheduler from at least trying the rest.
            logger.exception("Tick for %s raised; other companies still ran this cycle.", slug)
            result.failed.append(slug)
            continue

        result.summaries[slug] = summary
        if summary.halted_by:
            logger.warning(
                "%s: stopped by %s, nothing planned or dispatched. Clear it on the "
                "dashboard's budget page to resume.",
                slug,
                summary.halted_by,
            )
            continue
        logger.info(
            "%s: planner=%s proposed=%d enqueued=%d deduped_out=%d requeued=%d dispatched=%d",
            slug,
            summary.planner_status.value if summary.planner_status else "n/a",
            summary.proposed,
            summary.enqueued,
            summary.deduped_out,
            summary.requeued,
            summary.dispatched,
        )
        if summary.planner_status not in (None, RunStatus.SUCCEEDED):
            logger.warning(
                "%s: the planner run ended %s, so this cycle planned nothing: %s",
                slug,
                summary.planner_status.value,
                (summary.planner_error or "no error recorded")[:300],
            )

    _reclaim_node_modules(session_factory, app_config, workspace_root)
    return result


def _flatten_sub_units(
    session: Session, company_id: int, proposals: list[ProposedTask]
) -> list[ProposedTask]:
    """Expand each proposal's ``sub_units``, if any, into their own top-level
    proposals, so dedup and enqueue treat every independent unit of work as
    its own task rather than one oversized task that can only fail as a
    whole (the disguised-multi-unit-task pattern: a single proposal covering
    several unrelated sources, none of which converges before the run
    ceiling, because there was never a task boundary between them).

    A proposal with no ``sub_units`` passes through unchanged. One with
    sub_units disappears entirely in favour of one synthetic
    :class:`ProposedTask` per sub-unit -- the parent itself is never
    enqueued once split, only its pieces are, each inheriting the parent's
    ``type``/``goal_key``/``rationale``/``priority`` so they still sort and
    dedup sensibly, but with their own title and description as the planner
    wrote them.
    """
    flattened: list[ProposedTask] = []
    for proposal in proposals:
        if not proposal.sub_units:
            flattened.append(proposal)
            continue
        children = [
            ProposedTask(
                type=proposal.type,
                title=unit.title,
                description=unit.description,
                goal_key=proposal.goal_key,
                rationale=proposal.rationale,
                priority=proposal.priority,
            )
            for unit in proposal.sub_units
        ]
        flattened.extend(children)
        log(
            session,
            company_id=company_id,
            kind=ActivityKind.TASK_SPLIT,
            summary=f"Split into {len(children)}: {proposal.title}",
            detail={"units": [child.title for child in children]},
        )
    return flattened


async def _enqueue_proposals(
    session: Session,
    app_config: AppConfig,
    runner: AgentRunner,
    company_id: int,
    plan: PlannerOutput,
    summary: TickSummary,
) -> None:
    summary.proposed = len(plan.tasks)
    flattened = _flatten_sub_units(session, company_id, plan.tasks)
    capped = sorted(flattened, key=lambda t: t.priority)[: app_config.limits.max_tasks_per_tick]

    decisions = await deduplicate(
        session, runner, company_id=company_id, config=app_config.dedup, proposals=capped
    )

    today = utcday()
    created_today = session.execute(
        select(func.count(Task.id)).where(
            Task.company_id == company_id,
            func.strftime("%Y-%m-%d", Task.created_at) == today,
        )
    ).scalar_one()
    remaining_today = max(0, app_config.limits.max_tasks_per_day - created_today)

    goals_by_key = {
        goal.key: goal
        for goal in session.execute(select(Goal).where(Goal.company_id == company_id)).scalars()
    }

    for proposal, decision in zip(capped, decisions, strict=True):
        if not decision.keep:
            summary.deduped_out += 1
            log(
                session,
                company_id=company_id,
                kind=ActivityKind.TASK_DEDUPED,
                summary=f"Dropped as duplicate: {proposal.title}",
                detail={
                    "duplicate_of_task_id": decision.duplicate_of_task_id,
                    "note": decision.dedup_note,
                },
            )
            continue

        if remaining_today <= 0:
            summary.skipped_daily_cap += 1
            log(
                session,
                company_id=company_id,
                kind=ActivityKind.TASK_DEDUPED,
                summary=f"Skipped, daily task cap reached: {proposal.title}",
                detail={"max_tasks_per_day": app_config.limits.max_tasks_per_day},
            )
            continue

        goal = goals_by_key.get(proposal.goal_key)
        if goal is None:
            log(
                session,
                company_id=company_id,
                kind=ActivityKind.ERROR,
                summary=f"Planner proposed a task against unknown goal_key {proposal.goal_key!r}",
                error=f"No goal with key {proposal.goal_key!r} exists for this company.",
            )
            continue

        task = Task(
            company_id=company_id,
            goal_id=goal.id,
            type=proposal.type,
            title=proposal.title,
            description=proposal.description,
            rationale=proposal.rationale,
            priority=proposal.priority,
            dedup_key=decision.dedup_key,
            dedup_note=decision.dedup_note,
        )
        session.add(task)
        session.flush()
        remaining_today -= 1
        summary.enqueued += 1
        log(
            session,
            company_id=company_id,
            kind=ActivityKind.TASK_ENQUEUED,
            summary=f"Enqueued: {task.title}",
            task_id=task.id,
            detail={"rationale": proposal.rationale, "priority": proposal.priority},
        )

    session.commit()


def _requeue_eligible_failures(
    session: Session, limits: LimitsConfig, company_id: int, summary: TickSummary
) -> None:
    now = utcnow()
    failed = session.execute(
        select(Task).where(Task.company_id == company_id, Task.state == TaskState.FAILED)
    ).scalars()

    for task in failed:
        if not is_retry_eligible(task, limits, now):
            continue
        task.transition_to(TaskState.QUEUED)
        summary.requeued += 1
        log(
            session,
            company_id=company_id,
            kind=ActivityKind.TASK_STATE_CHANGED,
            summary=(
                f"Requeued for retry (attempt {task.attempts + 1}/{limits.max_attempts}): "
                f"{task.title}"
            ),
            task_id=task.id,
        )
    session.commit()


async def _dispatch_one(
    session_factory: sessionmaker[Session],
    runner: AgentRunner,
    company_profile: CompanyProfile,
    workspace_root: Path,
    task_id: int,
) -> None:
    """Run exactly one task to completion, on its own session.

    Defensive against a task that has already moved on by the time this runs (it
    should not, since capacity was sized against a snapshot moments earlier, but two
    ticks could in principle overlap if ``scheduler.coalesce`` were off): a task not
    still ``queued`` is left alone rather than forced through the state machine.
    """
    with session_factory() as session:
        task = session.get(Task, task_id, options=[selectinload(Task.goal)])
        if task is None or task.state != TaskState.QUEUED:
            return

        agent_name = TASK_TYPE_TO_AGENT[task.type]
        user_prompt = _build_worker_prompt(task, task.goal)

        workspace: Path | None = None
        if task.type in _TASK_TYPES_WITH_A_WORKSPACE:
            # A read-only clone of the company's repo, if one is configured, so
            # engineering and research tasks alike have real code to read instead
            # of an empty directory. See workspace.py for how the credential is
            # kept out of its reach. Only engineering also gets a writable copy
            # (work/, alongside the read-only repo/): research never edits
            # anything, so the copy would just be a wasted cost it structurally
            # cannot use.
            try:
                workspace = prepare_task_workspace(
                    company_profile,
                    workspace_root,
                    task.id,
                    needs_writable_copy=task.type is TaskType.ENGINEERING,
                )
            except (WorkspaceError, OSError) as exc:
                # OSError too: a filesystem problem preparing this one task's
                # workspace is this task's failure, counted toward abandonment, not
                # a reason to leave it queued to fail identically on every tick and
                # take the whole company's tick down with it (found live, 2 Oct 2026).
                #
                # The task never reached the agent, but it was still a real
                # attempt (a broken repo slug, a dead token, a network outage),
                # and it must count as one: transition through running first so
                # fail_or_abandon sees the same attempts/cost picture it always
                # does, rather than a bespoke path that could loop forever on a
                # persistently wrong profile setting.
                task.transition_to(TaskState.RUNNING)
                session.commit()
                runner.fail_or_abandon(session, task, str(exc))
                return

        await runner.run_worker(
            session,
            agent_name=agent_name,
            task=task,
            company_profile=company_profile,
            user_prompt=user_prompt,
            workspace=workspace,
        )


def run_startup_recovery(session_factory: sessionmaker[Session], app_config: AppConfig) -> None:
    """Call exactly once, before the scheduler's first tick.

    Recovers any run left ``running`` by a process that died before it could
    record what happened (see ``polska.budget.reconcile_orphaned_runs``), then any
    task that process left ``running`` too.
    """
    with session_factory() as session:
        orphans = reconcile_orphaned_runs(session, app_config)
        interrupted = _reconcile_interrupted_tasks(session, app_config)
    if interrupted:
        logger.warning(
            "Moved %d task(s) left running by an interrupted process out of running.",
            interrupted,
        )
    if orphans:
        logger.warning(
            "Recovered %d orphaned run(s) from an interrupted process, priced at "
            "their reservation's worst case. Check the activity feed and, once the "
            "real cost is known, consider polska.budget.write_off_orphan.",
            len(orphans),
        )


def _reconcile_interrupted_tasks(session: Session, app_config: AppConfig) -> int:
    """Move every task left in ``running`` by a process that died mid-run out of it.

    Only called at process start (and under the tick lock for a CLI tick), when no run
    of this process can be in flight, so a ``running`` task here can only belong to a
    process that was killed: a deploy recreating the container mid-tick is enough.
    Nothing else ever moves a task out of ``running``, so before this it stayed there
    for good, counting against ``max_concurrent_tasks``. Its attempt was already
    counted when it entered ``running``.
    """
    limits = app_config.limits
    reason = "Interrupted by a process restart before its run finished."
    stuck = session.execute(select(Task).where(Task.state == TaskState.RUNNING)).scalars().all()
    for task in stuck:
        if task.attempts >= limits.max_attempts:
            task.transition_to(
                TaskState.ABANDONED,
                result={
                    "abandoned_reason": reason,
                    "abandoned_because": f"attempts ({task.attempts}/{limits.max_attempts})",
                    "attempts": task.attempts,
                },
            )
        else:
            task.transition_to(TaskState.FAILED, error=reason)
        log(
            session,
            company_id=task.company_id,
            kind=ActivityKind.TASK_STATE_CHANGED,
            summary=f"{reason} Now {task.state.value}: {task.title}",
            task_id=task.id,
        )
    if stuck:
        session.commit()
    return len(stuck)
