"""The multi-company tick cycle: error isolation, housekeeping order, and what it reports.

``main()`` itself is thin scheduler wiring, not tested here: there is nothing in it
to get wrong that isn't already covered by ``run_startup_recovery`` and
``run_company_tick``'s own tests. What is worth proving is that one company's
failure, at either stage, does not take the others down with it, that housekeeping
can never stop a company being planned for, and that a planner whose output was
rejected is reported as that rather than as a quiet cycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from polska.budget import BudgetGuard
from polska.config.appconfig import AppConfig, load_app_config
from polska.db.enums import RunStatus
from polska.db.models import Company
from polska.main import _tick_all_companies
from polska.orchestrator import UnknownCompanyError, run_tick_cycle
from polska.runner import InvocationResult
from polska.schemas.planner import PlannerOutput


def _write_profile(directory: Path, slug: str, *, malformed: bool = False) -> None:
    if malformed:
        (directory / f"{slug}.yaml").write_text("not: [valid, yaml: at all", encoding="utf-8")
        return
    (directory / f"{slug}.yaml").write_text(
        f"""
slug: {slug}
name: {slug.title()}
idea: Testing the multi-company tick loop.
goals: []
""",
        encoding="utf-8",
    )


def _fake_run(status: RunStatus = RunStatus.SUCCEEDED, error: str | None = None):
    return type("FakeRun", (), {"id": 1, "error": error, "status": status})()


class _EmptyPlanRunner:
    """Just enough of AgentRunner's surface for a tick that proposes nothing.

    ``budget_guard`` is real because the planner prompt quotes it. Without one, a
    "healthy" company's tick raised AttributeError inside run_company_tick and was
    swallowed by the very isolation under test, so the older assertions (a company
    row exists) passed without a tick ever completing.
    """

    def __init__(self) -> None:
        self.budget_guard = BudgetGuard(
            load_app_config(Path(__file__).resolve().parents[1] / "config" / "default.yaml")
        )

    async def run_planner(self, session, **kwargs):
        output = PlannerOutput(tasks=[], no_action_reason="Nothing to do.")
        return InvocationResult(output=output, run=_fake_run())


class _ExplodingRunner(_EmptyPlanRunner):
    async def run_planner(self, session, **kwargs):
        raise RuntimeError("the CLI blew up for this company specifically")


class _RejectedPlanRunner(_EmptyPlanRunner):
    async def run_planner(self, session, **kwargs):
        return InvocationResult(
            output=None,
            run=_fake_run(RunStatus.INVALID_OUTPUT, "returned no tasks and no no_action_reason"),
        )


@pytest.fixture
def companies_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "companies"
    directory.mkdir()
    return directory


async def test_a_malformed_profile_does_not_stop_the_others(
    session_factory, app_config: AppConfig, companies_dir: Path, tmp_path: Path
) -> None:
    _write_profile(companies_dir, "broken", malformed=True)
    _write_profile(companies_dir, "healthy")

    await _tick_all_companies(
        session_factory, app_config, _EmptyPlanRunner(), companies_dir, tmp_path / "workspaces"
    )

    with session_factory() as session:
        slugs = {c.slug for c in session.execute(select(Company)).scalars()}
    # The broken one never got far enough to create a row; the healthy one did.
    assert slugs == {"healthy"}


async def test_one_companys_tick_raising_does_not_stop_the_next(
    session_factory, app_config: AppConfig, companies_dir: Path, tmp_path: Path
) -> None:
    _write_profile(companies_dir, "aaa-explodes")
    _write_profile(companies_dir, "bbb-healthy")

    await _tick_all_companies(
        session_factory, app_config, _ExplodingRunner(), companies_dir, tmp_path / "workspaces"
    )

    # Both got as far as sync (a company row exists for each), proving the second
    # ran at all despite the first's run_planner raising.
    with session_factory() as session:
        slugs = {c.slug for c in session.execute(select(Company)).scalars()}
    assert slugs == {"aaa-explodes", "bbb-healthy"}


async def test_the_cycle_reports_which_companies_failed(
    session_factory, app_config: AppConfig, companies_dir: Path, tmp_path: Path
) -> None:
    _write_profile(companies_dir, "broken", malformed=True)
    _write_profile(companies_dir, "aaa-explodes")

    result = await run_tick_cycle(
        session_factory, app_config, _ExplodingRunner(), companies_dir, tmp_path / "workspaces"
    )

    assert sorted(result.failed) == ["aaa-explodes", "broken"]
    assert result.summaries == {}


async def test_a_housekeeping_failure_does_not_stop_companies_being_planned(
    session_factory, app_config: AppConfig, companies_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    """The 1 and 2 Oct 2026 incident: cleanup raised ahead of every company, so
    nothing was planned for either day."""
    _write_profile(companies_dir, "healthy")

    def boom(*args, **kwargs):
        raise OSError("something housekeeping could not touch")

    monkeypatch.setattr("polska.orchestrator.reconcile_removed_companies", boom)
    monkeypatch.setattr("polska.orchestrator.reclaim_node_modules_for_terminal_tasks", boom)

    result = await run_tick_cycle(
        session_factory, app_config, _EmptyPlanRunner(), companies_dir, tmp_path / "workspaces"
    )

    assert list(result.summaries) == ["healthy"]
    assert result.failed == []


async def test_housekeeping_brackets_the_company_ticks_even_when_one_raises(
    session_factory, app_config: AppConfig, companies_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "polska.orchestrator.reconcile_removed_companies",
        lambda session, directory: calls.append("reconcile") or 0,
    )
    monkeypatch.setattr(
        "polska.orchestrator.reclaim_node_modules_for_terminal_tasks",
        lambda session, root, grace_period: calls.append("reclaim") or 0,
    )

    class Recording(_ExplodingRunner):
        async def run_planner(self, session, **kwargs):
            calls.append("plan")
            return await super().run_planner(session, **kwargs)

    _write_profile(companies_dir, "aaa-explodes")

    await run_tick_cycle(
        session_factory, app_config, Recording(), companies_dir, tmp_path / "workspaces"
    )

    assert calls == ["reconcile", "plan", "reclaim"]


async def test_a_rejected_planner_run_is_reported_not_passed_off_as_a_quiet_cycle(
    session_factory, app_config: AppConfig, companies_dir: Path, tmp_path: Path
) -> None:
    _write_profile(companies_dir, "co")

    result = await run_tick_cycle(
        session_factory, app_config, _RejectedPlanRunner(), companies_dir, tmp_path / "workspaces"
    )

    summary = result.summaries["co"]
    assert summary.proposed == 0
    assert summary.planner_status == RunStatus.INVALID_OUTPUT
    assert "no_action_reason" in summary.planner_error
    assert result.planner_problems == ["co"]


async def test_a_planner_that_chose_to_do_nothing_is_not_a_problem(
    session_factory, app_config: AppConfig, companies_dir: Path, tmp_path: Path
) -> None:
    _write_profile(companies_dir, "co")

    result = await run_tick_cycle(
        session_factory, app_config, _EmptyPlanRunner(), companies_dir, tmp_path / "workspaces"
    )

    assert result.summaries["co"].planner_status == RunStatus.SUCCEEDED
    assert result.planner_problems == []


async def test_only_slug_ticks_just_that_company(
    session_factory, app_config: AppConfig, companies_dir: Path, tmp_path: Path
) -> None:
    _write_profile(companies_dir, "aaa")
    _write_profile(companies_dir, "bbb")

    result = await run_tick_cycle(
        session_factory,
        app_config,
        _EmptyPlanRunner(),
        companies_dir,
        tmp_path / "workspaces",
        only_slug="aaa",
    )

    assert list(result.summaries) == ["aaa"]


async def test_an_unknown_slug_raises_before_any_housekeeping_runs(
    session_factory, app_config: AppConfig, companies_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    _write_profile(companies_dir, "real")
    calls: list[str] = []
    monkeypatch.setattr(
        "polska.orchestrator.reconcile_removed_companies",
        lambda session, directory: calls.append("reconcile") or 0,
    )

    with pytest.raises(UnknownCompanyError):
        await run_tick_cycle(
            session_factory,
            app_config,
            _EmptyPlanRunner(),
            companies_dir,
            tmp_path / "workspaces",
            only_slug="nope",
        )

    assert calls == []
