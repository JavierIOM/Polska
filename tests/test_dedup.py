"""Deduplication: the deterministic pass, the state rules, and the judge tiebreak.

The state rules (FAILED suppresses forever, DONE suppresses only inside the lookback
window, ABANDONED never suppresses) are the fix from review for the failure mode
where a genuinely unmet need could go quiet forever. These tests prove the *matching*
code actually honours those rules, not just that the state sets themselves partition
correctly (that is ``test_task_state_machine.py``'s job).
"""

from __future__ import annotations

import datetime as dt

import pytest
from claude_agent_sdk import ResultMessage
from sqlalchemy.orm import Session

from polska.adapters.registry import AdapterRegistry
from polska.budget import BudgetGuard
from polska.config.appconfig import AppConfig, DedupConfig
from polska.db.enums import GoalStatus, TaskState, TaskType
from polska.db.models import Company, Goal, Task
from polska.db.types import utcnow
from polska.dedup import deduplicate
from polska.runner import AgentRunner
from polska.schemas.planner import ProposedTask


def _goal(session: Session, company: Company, key: str = "growth") -> Goal:
    goal = Goal(
        company_id=company.id,
        key=key,
        title="Grow the list",
        metric="subs",
        target_value=500,
        status=GoalStatus.ACTIVE,
    )
    session.add(goal)
    session.flush()
    return goal


def _task(
    session: Session,
    company: Company,
    goal: Goal,
    *,
    title: str,
    state: TaskState,
    created_at: dt.datetime | None = None,
) -> Task:
    task = Task(
        company_id=company.id,
        goal_id=goal.id,
        type=TaskType.MARKETING,
        title=title,
        description="",
        rationale="",
        state=state,
    )
    if created_at is not None:
        task.created_at = created_at
    session.add(task)
    session.commit()
    return task


def _proposal(**overrides: object) -> ProposedTask:
    base: dict[str, object] = {
        "type": TaskType.MARKETING,
        "title": "Draft the launch announcement",
        "description": "Write the post announcing the new range.",
        "goal_key": "growth",
        "rationale": "The range is live and nothing has been said about it.",
    }
    base.update(overrides)
    return ProposedTask.model_validate(base)


def _judge_query(*verdicts: dict[str, object]):
    async def fake(*, prompt: str, options: object):
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            structured_output={"verdicts": list(verdicts)},
            model_usage={
                "claude-haiku-4-5": {"inputTokens": 10, "outputTokens": 5, "costUSD": 0.0001}
            },
        )

    return fake


def _failing_judge_query():
    async def fake(*, prompt: str, options: object):
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            structured_output=None,
        )

    return fake


@pytest.fixture
def runner(app_config: AppConfig) -> AgentRunner:
    """A runner whose judge call would fail if actually invoked; tests that need
    the judge override query_fn on a fresh instance."""
    return AgentRunner(
        app_config=app_config,
        budget_guard=BudgetGuard(app_config),
        adapter_registry=AdapterRegistry(),
        query_fn=_failing_judge_query(),
    )


def _dedup_config() -> DedupConfig:
    return DedupConfig(high_threshold=90, low_threshold=60, lookback_days=14)


# ------------------------------------------------------------------------ novel


async def test_a_proposal_with_no_candidates_is_kept(
    session: Session, company: Company, runner: AgentRunner
) -> None:
    _goal(session, company)
    decisions = await deduplicate(
        session, runner, company_id=company.id, config=_dedup_config(), proposals=[_proposal()]
    )
    assert decisions[0].keep
    assert "No comparable" in decisions[0].dedup_note


async def test_a_clearly_different_proposal_is_kept_as_novel(
    session: Session, company: Company, runner: AgentRunner
) -> None:
    goal = _goal(session, company)
    _task(session, company, goal, title="Fix the checkout bug", state=TaskState.QUEUED)
    decisions = await deduplicate(
        session,
        runner,
        company_id=company.id,
        config=_dedup_config(),
        proposals=[_proposal(title="Draft the launch announcement")],
    )
    assert decisions[0].keep


# ------------------------------------------------------------------- suppression


async def test_an_identical_queued_task_suppresses_the_proposal(
    session: Session, company: Company, runner: AgentRunner
) -> None:
    goal = _goal(session, company)
    existing = _task(
        session, company, goal, title="Draft the launch announcement", state=TaskState.QUEUED
    )
    decisions = await deduplicate(
        session, runner, company_id=company.id, config=_dedup_config(), proposals=[_proposal()]
    )
    assert not decisions[0].keep
    assert decisions[0].duplicate_of_task_id == existing.id


async def test_a_failed_task_suppresses_unconditionally_however_old(
    session: Session, company: Company, runner: AgentRunner
) -> None:
    """FAILED is the orchestrator's own retry queue: a fresh proposal for the same
    work would race a pending retry rather than replace it, regardless of age."""
    goal = _goal(session, company)
    ancient = utcnow() - dt.timedelta(days=365)
    existing = _task(
        session,
        company,
        goal,
        title="Draft the launch announcement",
        state=TaskState.FAILED,
        created_at=ancient,
    )
    decisions = await deduplicate(
        session, runner, company_id=company.id, config=_dedup_config(), proposals=[_proposal()]
    )
    assert not decisions[0].keep
    assert decisions[0].duplicate_of_task_id == existing.id


async def test_a_done_task_outside_the_lookback_window_no_longer_suppresses(
    session: Session, company: Company, runner: AgentRunner
) -> None:
    goal = _goal(session, company)
    old = utcnow() - dt.timedelta(days=30)
    _task(
        session,
        company,
        goal,
        title="Draft the launch announcement",
        state=TaskState.DONE,
        created_at=old,
    )
    decisions = await deduplicate(
        session,
        runner,
        company_id=company.id,
        config=_dedup_config(),  # lookback_days=14, this task is 30 days old
        proposals=[_proposal()],
    )
    assert decisions[0].keep


async def test_a_done_task_inside_the_lookback_window_suppresses(
    session: Session, company: Company, runner: AgentRunner
) -> None:
    goal = _goal(session, company)
    recent = utcnow() - dt.timedelta(days=2)
    existing = _task(
        session,
        company,
        goal,
        title="Draft the launch announcement",
        state=TaskState.DONE,
        created_at=recent,
    )
    decisions = await deduplicate(
        session, runner, company_id=company.id, config=_dedup_config(), proposals=[_proposal()]
    )
    assert not decisions[0].keep
    assert decisions[0].duplicate_of_task_id == existing.id


async def test_an_abandoned_task_never_suppresses_however_identical_and_recent(
    session: Session, company: Company, runner: AgentRunner
) -> None:
    """The one state that must never haunt a future proposal: the system tried and
    gave up, so the underlying need is still open."""
    goal = _goal(session, company)
    just_now = utcnow() - dt.timedelta(minutes=5)
    _task(
        session,
        company,
        goal,
        title="Draft the launch announcement",
        state=TaskState.ABANDONED,
        created_at=just_now,
    )
    decisions = await deduplicate(
        session, runner, company_id=company.id, config=_dedup_config(), proposals=[_proposal()]
    )
    assert decisions[0].keep
    assert "No comparable" in decisions[0].dedup_note


# ------------------------------------------------------------------------- judge


async def test_an_ambiguous_score_with_judge_disabled_defaults_to_kept(
    session: Session, company: Company, app_config: AppConfig
) -> None:
    goal = _goal(session, company)
    _task(
        session,
        company,
        goal,
        title="Announce the product launch to customers",  # similar but not identical
        state=TaskState.QUEUED,
    )
    config = DedupConfig(high_threshold=95, low_threshold=10, judge_enabled=False)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=BudgetGuard(app_config),
        adapter_registry=AdapterRegistry(),
        query_fn=_failing_judge_query(),  # must never be called
    )
    decisions = await deduplicate(
        session, runner, company_id=company.id, config=config, proposals=[_proposal()]
    )
    assert decisions[0].keep
    assert "judge disabled" in decisions[0].dedup_note


async def test_the_judge_can_mark_an_ambiguous_proposal_a_duplicate(
    session: Session, company: Company, app_config: AppConfig
) -> None:
    goal = _goal(session, company)
    existing = _task(
        session,
        company,
        goal,
        title="Announce the product launch to customers",
        state=TaskState.QUEUED,
    )
    config = DedupConfig(high_threshold=95, low_threshold=10, judge_enabled=True)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=BudgetGuard(app_config),
        adapter_registry=AdapterRegistry(),
        query_fn=_judge_query(
            {
                "index": 0,
                "is_duplicate": True,
                "duplicate_of_task_id": existing.id,
                "reason": "Same underlying work, different wording.",
            }
        ),
    )
    decisions = await deduplicate(
        session, runner, company_id=company.id, config=config, proposals=[_proposal()]
    )
    assert not decisions[0].keep
    assert decisions[0].duplicate_of_task_id == existing.id
    assert "Same underlying work" in decisions[0].dedup_note


async def test_the_judge_can_mark_an_ambiguous_proposal_novel(
    session: Session, company: Company, app_config: AppConfig
) -> None:
    goal = _goal(session, company)
    _task(
        session,
        company,
        goal,
        title="Announce the product launch to customers",
        state=TaskState.QUEUED,
    )
    config = DedupConfig(high_threshold=95, low_threshold=10, judge_enabled=True)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=BudgetGuard(app_config),
        adapter_registry=AdapterRegistry(),
        query_fn=_judge_query(
            {"index": 0, "is_duplicate": False, "reason": "Different campaign entirely."}
        ),
    )
    decisions = await deduplicate(
        session, runner, company_id=company.id, config=config, proposals=[_proposal()]
    )
    assert decisions[0].keep
    assert "Different campaign" in decisions[0].dedup_note


async def test_a_judge_call_with_no_usable_output_defaults_to_kept(
    session: Session, company: Company, app_config: AppConfig
) -> None:
    goal = _goal(session, company)
    _task(
        session,
        company,
        goal,
        title="Announce the product launch to customers",
        state=TaskState.QUEUED,
    )
    config = DedupConfig(high_threshold=95, low_threshold=10, judge_enabled=True)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=BudgetGuard(app_config),
        adapter_registry=AdapterRegistry(),
        query_fn=_failing_judge_query(),
    )
    decisions = await deduplicate(
        session, runner, company_id=company.id, config=config, proposals=[_proposal()]
    )
    assert decisions[0].keep
    assert "no usable verdict" in decisions[0].dedup_note


async def test_decisions_align_with_proposals_by_index_across_a_mixed_batch(
    session: Session, company: Company, app_config: AppConfig
) -> None:
    goal = _goal(session, company)
    duplicate_target = _task(
        session, company, goal, title="Draft the launch announcement", state=TaskState.QUEUED
    )
    config = _dedup_config()
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=BudgetGuard(app_config),
        adapter_registry=AdapterRegistry(),
        query_fn=_failing_judge_query(),
    )
    proposals = [
        _proposal(title="Draft the launch announcement"),  # duplicate
        _proposal(title="Fix the checkout bug entirely unrelated topic"),  # novel
    ]
    decisions = await deduplicate(
        session, runner, company_id=company.id, config=config, proposals=proposals
    )
    assert len(decisions) == 2
    assert not decisions[0].keep
    assert decisions[0].duplicate_of_task_id == duplicate_target.id
    assert decisions[1].keep
