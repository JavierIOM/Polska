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
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from polska.activity import log
from polska.budget import reconcile_orphaned_runs
from polska.config.appconfig import AppConfig, LimitsConfig
from polska.config.company import CompanyProfile, LoadedProfile
from polska.db.enums import ActivityKind, AgentName, GoalStatus, TaskState, TaskType
from polska.db.models import Goal, Task
from polska.db.state import ACTIVE_STATES
from polska.db.types import utcday, utcnow
from polska.dedup import deduplicate
from polska.runner import AgentRunner
from polska.schemas.planner import PlannerOutput
from polska.sync import sync_company

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
_CONCLUDED_STATES = frozenset({TaskState.DONE, TaskState.FAILED, TaskState.ABANDONED})


class TickSummary:
    """What happened in one company's tick. Returned for logging and for tests to
    assert against, rather than having to re-derive it from the activity feed."""

    __slots__ = (
        "company_id",
        "skipped_inactive",
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


def _build_planner_prompt(session: Session, company_id: int, open_goals: list[Goal]) -> str:
    lines = ["Open goals:"]
    if not open_goals:
        lines.append("  (none open)")
    for goal in open_goals:
        lines.append(
            f"  - [{goal.key}] {goal.title}: {goal.current_value:g}/{goal.target_value:g} "
            f"{goal.unit} ({goal.progress:.0%}), priority {goal.priority}"
        )

    recent = session.execute(
        select(Task)
        .where(Task.company_id == company_id, Task.state.in_(_CONCLUDED_STATES))
        .order_by(Task.updated_at.desc())
        .limit(10)
    ).scalars()

    lines.append("")
    lines.append("Recent outcomes, most recent first:")
    any_recent = False
    for task in recent:
        any_recent = True
        outcome = task.error or (task.result or {}).get("summary", "") or ""
        lines.append(f"  - [{task.state.value}] {task.title}: {outcome}"[:300])
    if not any_recent:
        lines.append("  (none yet)")

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
    return "\n".join(lines)


def _build_worker_prompt(task: Task, goal: Goal | None) -> str:
    parts = [f"Task: {task.title}", "", task.description or "(no further description given)"]
    if task.rationale:
        parts.append(f"\nWhy this task exists: {task.rationale}")
    if goal is not None:
        parts.append(
            f"\nThis serves the goal '{goal.title}': currently {goal.current_value:g}/"
            f"{goal.target_value:g} {goal.unit}."
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
        planner_prompt = _build_planner_prompt(session, company.id, open_goals)
        plan_outcome = await runner.run_planner(
            session, company_id=company.id, company_profile=profile, user_prompt=planner_prompt
        )
        plan = plan_outcome.output

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


async def _enqueue_proposals(
    session: Session,
    app_config: AppConfig,
    runner: AgentRunner,
    company_id: int,
    plan: PlannerOutput,
    summary: TickSummary,
) -> None:
    summary.proposed = len(plan.tasks)
    capped = sorted(plan.tasks, key=lambda t: t.priority)[: app_config.limits.max_tasks_per_tick]

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
        if task.type == TaskType.ENGINEERING:
            workspace = workspace_root / str(task.id)
            workspace.mkdir(parents=True, exist_ok=True)

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
    record what happened. See ``polska.budget.reconcile_orphaned_runs``.
    """
    with session_factory() as session:
        orphans = reconcile_orphaned_runs(session, app_config)
    if orphans:
        logger.warning(
            "Recovered %d orphaned run(s) from an interrupted process, priced at "
            "their reservation's worst case. Check the activity feed and, once the "
            "real cost is known, consider polska.budget.write_off_orphan.",
            len(orphans),
        )
