"""The multi-company tick loop's error isolation.

``main()`` itself is thin scheduler wiring, not tested here: there is nothing in it
to get wrong that isn't already covered by ``run_startup_recovery`` and
``run_company_tick``'s own tests. What is worth proving is that one company's
failure, at either stage, does not take the others down with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from polska.config.appconfig import AppConfig
from polska.db.models import Company
from polska.main import _tick_all_companies


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


class _EmptyPlanRunner:
    """Just enough of AgentRunner's surface for a tick that proposes nothing."""

    async def run_planner(self, session, **kwargs):

        from polska.runner import InvocationResult
        from polska.schemas.planner import PlannerOutput

        output = PlannerOutput(tasks=[], no_action_reason="Nothing to do.")
        fake_run = type("FakeRun", (), {"id": 1, "error": None})()
        return InvocationResult(output=output, run=fake_run)


class _ExplodingRunner(_EmptyPlanRunner):
    async def run_planner(self, session, **kwargs):
        raise RuntimeError("the CLI blew up for this company specifically")


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
