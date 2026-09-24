"""The orchestrator loop: retry timing, and a full tick end to end against fakes.

Nothing here reaches the network. The planner and worker agents are driven through
``AgentRunner``'s injected ``query_fn``, exactly as ``test_runner.py`` does; a tick is
a sequence of those calls plus the database bookkeeping around them, so this is
where that sequence is proven, not any one call in isolation.
"""

from __future__ import annotations

import datetime as dt

import pytest
from claude_agent_sdk import CLIConnectionError, ResultMessage
from sqlalchemy import select

from polska.adapters.registry import AdapterRegistry
from polska.budget import BudgetGuard
from polska.config.appconfig import AppConfig, LimitsConfig
from polska.config.company import CompanyProfile, LoadedProfile, profile_hash
from polska.db.enums import ActivityKind, TaskState, TaskType
from polska.db.models import ActivityEvent, Company, Goal, Task
from polska.db.types import utcnow
from polska.orchestrator import (
    is_retry_eligible,
    retry_delay_seconds,
    run_company_tick,
    run_startup_recovery,
)
from polska.runner import AgentRunner

# --------------------------------------------------------------------- backoff


def test_retry_delay_is_the_base_on_the_first_attempt() -> None:
    limits = LimitsConfig(retry_base_delay_seconds=900, retry_backoff_multiplier=2.0)
    assert retry_delay_seconds(1, limits) == 900


def test_retry_delay_doubles_each_attempt() -> None:
    limits = LimitsConfig(retry_base_delay_seconds=900, retry_backoff_multiplier=2.0)
    assert retry_delay_seconds(2, limits) == 1800
    assert retry_delay_seconds(3, limits) == 3600


def test_retry_delay_is_capped() -> None:
    limits = LimitsConfig(
        retry_base_delay_seconds=900, retry_backoff_multiplier=2.0, retry_max_delay_seconds=1000
    )
    assert retry_delay_seconds(3, limits) == 1000


def test_a_non_failed_task_is_never_retry_eligible(task: Task) -> None:
    limits = LimitsConfig()
    assert task.state == TaskState.QUEUED
    assert not is_retry_eligible(task, limits, utcnow())


def test_a_failed_task_before_its_backoff_elapses_is_not_eligible(task: Task) -> None:
    limits = LimitsConfig(retry_base_delay_seconds=900)
    task.transition_to(TaskState.RUNNING, now=utcnow() - dt.timedelta(seconds=901))
    task.transition_to(TaskState.FAILED, error="x", now=utcnow() - dt.timedelta(seconds=100))
    assert not is_retry_eligible(task, limits, utcnow())


def test_a_failed_task_after_its_backoff_elapses_is_eligible(task: Task) -> None:
    limits = LimitsConfig(retry_base_delay_seconds=900)
    long_ago = utcnow() - dt.timedelta(seconds=1000)
    task.transition_to(TaskState.RUNNING, now=long_ago)
    task.transition_to(TaskState.FAILED, error="x", now=long_ago)
    assert is_retry_eligible(task, limits, utcnow())


# ------------------------------------------------------------------------- tick


def _profile(**overrides: object) -> LoadedProfile:
    base: dict[str, object] = {
        "slug": "tick-co",
        "name": "Tick Co",
        "idea": "Selling things.",
        "goals": [
            {
                "key": "growth",
                "title": "Grow the list",
                "metric": "subs",
                "target": 500,
                "current": 10,
            }
        ],
    }
    base.update(overrides)
    profile = CompanyProfile.model_validate(base)
    raw = f"slug: {profile.slug}"
    return LoadedProfile(
        profile=profile, path="companies/tick-co.yaml", raw=raw, sha256=profile_hash(raw)
    )


def _planner_result(structured_output: object) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        structured_output=structured_output,
        model_usage={
            "claude-sonnet-5": {"inputTokens": 100, "outputTokens": 20, "costUSD": 0.0006}
        },
    )


def _worker_result(structured_output: object) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        structured_output=structured_output,
        model_usage={
            "claude-sonnet-5": {"inputTokens": 100, "outputTokens": 20, "costUSD": 0.0006}
        },
    )


class _ScriptedRunner:
    """Wraps a real AgentRunner but with a per-call-count query_fn, so a tick's
    sequence of calls (plan, then N workers) can each return something different
    without needing to build a single one-size-fits-all fake."""

    def __init__(self, app_config: AppConfig, plan_output: object, worker_outputs: list[object]):
        self._plan_output = plan_output
        self._worker_outputs = list(worker_outputs)
        self._registry = AdapterRegistry()
        self._budget = BudgetGuard(app_config)
        self._app_config = app_config

    def _make_runner(self, output: object) -> AgentRunner:
        async def fake(*, prompt: str, options: object):
            yield _worker_result(output)

        return AgentRunner(
            app_config=self._app_config,
            budget_guard=self._budget,
            adapter_registry=self._registry,
            query_fn=fake,
        )

    async def run_planner(self, session, **kwargs):
        return await self._make_runner(self._plan_output).run_planner(session, **kwargs)

    async def run_dedup_judge(self, session, **kwargs):
        return await self._make_runner({"verdicts": []}).run_dedup_judge(session, **kwargs)

    async def run_worker(self, session, **kwargs):
        output = (
            self._worker_outputs.pop(0)
            if self._worker_outputs
            else {
                "succeeded": True,
                "summary": "done",
                "output": {},
            }
        )
        return await self._make_runner(output).run_worker(session, **kwargs)


@pytest.fixture
def workspace_root(tmp_path):
    return tmp_path / "workspaces"


async def test_an_empty_plan_enqueues_nothing(
    session_factory, app_config: AppConfig, workspace_root
) -> None:
    plan = {"tasks": [], "no_action_reason": "Nothing new."}
    runner = _ScriptedRunner(app_config, plan, [])

    loaded = _profile()
    summary = await run_company_tick(session_factory, app_config, runner, loaded, workspace_root)

    assert summary.proposed == 0
    assert summary.enqueued == 0
    with session_factory() as session:
        assert session.execute(select(Task)).scalar_one_or_none() is None


async def test_a_proposed_task_is_enqueued_against_its_goal(
    session_factory, app_config: AppConfig, workspace_root
) -> None:
    plan = {
        "tasks": [
            {
                "type": "marketing",
                "title": "Draft the launch announcement",
                "description": "Write the post.",
                "goal_key": "growth",
                "rationale": "Nothing has been said yet.",
                "priority": 10,
            }
        ]
    }
    # Capacity zeroed so this test isolates enqueue-against-goal from the same
    # tick's dispatch, which is covered by its own tests below.
    config = app_config.model_copy(
        update={"limits": app_config.limits.model_copy(update={"max_concurrent_tasks": 0})}
    )
    runner = _ScriptedRunner(config, plan, [])

    loaded = _profile()
    summary = await run_company_tick(session_factory, config, runner, loaded, workspace_root)

    assert summary.proposed == 1
    assert summary.enqueued == 1
    assert summary.dispatched == 0
    with session_factory() as session:
        task = session.execute(select(Task)).scalar_one()
        assert task.title == "Draft the launch announcement"
        goal = session.execute(select(Goal)).scalar_one()
        assert task.goal_id == goal.id
        assert task.state == TaskState.QUEUED


async def test_an_inactive_company_is_skipped_entirely(
    session_factory, app_config: AppConfig, workspace_root
) -> None:
    plan = {"tasks": [], "no_action_reason": "should never be reached"}
    runner = _ScriptedRunner(app_config, plan, [])

    loaded = _profile(active=False)
    summary = await run_company_tick(session_factory, app_config, runner, loaded, workspace_root)

    assert summary.skipped_inactive
    with session_factory() as session:
        # sync_company still ran (the company row exists), but nothing else did.
        events = session.execute(select(ActivityEvent)).scalars().all()
        assert [e.kind for e in events] == [ActivityKind.SCHEDULER_TICK]


async def test_a_task_is_dispatched_up_to_max_concurrent_and_moves_to_done(
    session_factory, app_config: AppConfig, workspace_root
) -> None:
    config = app_config.model_copy(
        update={"limits": app_config.limits.model_copy(update={"max_concurrent_tasks": 2})}
    )
    plan = {
        "tasks": [
            {
                "type": "marketing",
                "title": "Draft post A",
                "description": "x",
                "goal_key": "growth",
                "rationale": "x",
                "priority": 10,
            },
            {
                "type": "research",
                "title": "Research topic B",
                "description": "x",
                "goal_key": "growth",
                "rationale": "x",
                "priority": 20,
            },
        ]
    }
    worker_outputs = [
        {"succeeded": True, "summary": "Drafted A.", "output": {}},
        {"succeeded": True, "summary": "Researched B.", "output": {}},
    ]
    runner = _ScriptedRunner(config, plan, worker_outputs)

    loaded = _profile()
    summary = await run_company_tick(session_factory, config, runner, loaded, workspace_root)

    assert summary.enqueued == 2
    assert summary.dispatched == 2
    with session_factory() as session:
        tasks = session.execute(select(Task).order_by(Task.priority)).scalars().all()
        assert [t.state for t in tasks] == [TaskState.DONE, TaskState.DONE]


async def test_dispatch_respects_max_concurrent_and_leaves_the_rest_queued(
    session_factory, app_config: AppConfig, workspace_root
) -> None:
    config = app_config.model_copy(
        update={"limits": app_config.limits.model_copy(update={"max_concurrent_tasks": 1})}
    )
    plan = {
        "tasks": [
            {
                "type": "marketing",
                "title": "Draft post A",
                "description": "x",
                "goal_key": "growth",
                "rationale": "x",
                "priority": 10,
            },
            {
                "type": "research",
                "title": "Research topic B",
                "description": "x",
                "goal_key": "growth",
                "rationale": "x",
                "priority": 20,
            },
        ]
    }
    runner = _ScriptedRunner(
        config, plan, [{"succeeded": True, "summary": "Drafted A.", "output": {}}]
    )

    loaded = _profile()
    summary = await run_company_tick(session_factory, config, runner, loaded, workspace_root)

    assert summary.enqueued == 2
    assert summary.dispatched == 1
    with session_factory() as session:
        tasks = session.execute(select(Task).order_by(Task.priority)).scalars().all()
        assert tasks[0].state == TaskState.DONE
        assert tasks[1].state == TaskState.QUEUED  # capacity was full


async def test_a_failed_task_past_its_backoff_is_requeued_and_redispatched(
    session_factory, app_config: AppConfig, workspace_root, company: Company
) -> None:
    """Uses the shared `company` fixture, whose goal key is 'grow-the-list', so the
    tick's own profile must match it to resolve goal_id -- irrelevant here since no
    new proposals are enqueued, only an existing FAILED task is retried."""
    config = app_config.model_copy(
        update={"limits": app_config.limits.model_copy(update={"retry_base_delay_seconds": 0})}
    )
    with session_factory() as session:
        goal = session.execute(select(Goal)).scalar_one()
        stale = Task(
            company_id=company.id,
            goal_id=goal.id,
            type=TaskType.MARKETING,
            title="Retry me",
            rationale="x",
        )
        session.add(stale)
        session.commit()
        stale.transition_to(TaskState.RUNNING)
        stale.transition_to(TaskState.FAILED, error="transient")
        session.commit()
        task_id = stale.id

    loaded = LoadedProfile(
        profile=CompanyProfile.model_validate(
            {
                "slug": company.slug,
                "name": company.name,
                "idea": company.idea,
                "goals": [
                    {
                        "key": "grow-the-list",
                        "title": "Grow the list",
                        "metric": "subs",
                        "target": 500,
                    }
                ],
            }
        ),
        path=company.profile_path,
        raw="x",
        sha256=company.profile_hash,  # unchanged hash: sync will not overwrite
    )
    plan = {"tasks": [], "no_action_reason": "Only testing retry."}
    runner = _ScriptedRunner(
        config, plan, [{"succeeded": True, "summary": "Retried ok.", "output": {}}]
    )

    summary = await run_company_tick(session_factory, config, runner, loaded, workspace_root)

    assert summary.requeued == 1
    assert summary.dispatched == 1
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task.state == TaskState.DONE
        assert task.attempts == 2


async def test_a_fatal_cli_error_during_dispatch_propagates_after_others_are_accounted_for(
    session_factory, app_config: AppConfig, workspace_root
) -> None:
    config = app_config.model_copy(
        update={"limits": app_config.limits.model_copy(update={"max_concurrent_tasks": 2})}
    )
    plan = {
        "tasks": [
            {
                "type": "marketing",
                "title": "Draft post A",
                "description": "x",
                "goal_key": "growth",
                "rationale": "x",
                "priority": 10,
            },
            {
                "type": "research",
                "title": "Research topic B",
                "description": "x",
                "goal_key": "growth",
                "rationale": "x",
                "priority": 20,
            },
        ]
    }

    class ExplodingRunner(_ScriptedRunner):
        async def run_worker(self, session, **kwargs):
            if "B" in kwargs["task"].title:
                raise CLIConnectionError("cli gone")
            return await super().run_worker(session, **kwargs)

    runner = ExplodingRunner(config, plan, [{"succeeded": True, "summary": "ok", "output": {}}])

    loaded = _profile()
    with pytest.raises(CLIConnectionError):
        await run_company_tick(session_factory, config, runner, loaded, workspace_root)

    with session_factory() as session:
        tasks = {t.title: t.state for t in session.execute(select(Task)).scalars()}
        assert tasks["Draft post A"] == TaskState.DONE  # accounted for despite the sibling's crash


# ------------------------------------------------------------------------- startup


def test_startup_recovery_logs_a_warning_when_orphans_are_found(
    session_factory, app_config: AppConfig, company: Company, caplog
) -> None:
    from polska.db.enums import RunStatus
    from polska.db.models import Run

    with session_factory() as session:
        session.add(
            Run(
                company_id=company.id,
                agent="analyst",
                model="claude-sonnet-5",
                status=RunStatus.RUNNING,
            )
        )
        session.commit()

    with caplog.at_level("WARNING", logger="polska.orchestrator"):
        run_startup_recovery(session_factory, app_config)

    assert any("orphaned run" in r.getMessage() for r in caplog.records)
    with session_factory() as session:
        run = session.execute(select(Run)).scalar_one()
        assert run.status == RunStatus.ORPHANED


def test_startup_recovery_is_silent_with_nothing_to_recover(
    session_factory, app_config: AppConfig, caplog
) -> None:
    with caplog.at_level("WARNING", logger="polska.orchestrator"):
        run_startup_recovery(session_factory, app_config)
    assert caplog.records == []
