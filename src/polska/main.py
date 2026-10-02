"""The process entrypoint: wires the scheduler and runs it forever.

Deliberately thin. Everything that has real logic in it (a tick, dedup, the budget
guard, the gate) lives in its own module and is tested there without touching this
file. This module's only job is: load config, build the shared objects once, recover
from any interrupted previous run, and then fire a tick on a schedule.

Does not run migrations. That stays a separate, explicit step
(``alembic upgrade head``) before this starts, the same as any other deployment: an
app auto-migrating itself on boot is a footgun this project does not want.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from polska.adapters.registry import AdapterRegistry
from polska.budget import BudgetGuard
from polska.config.appconfig import AppConfig, load_app_config
from polska.config.settings import load_settings
from polska.db.base import make_engine, make_session_factory
from polska.db.schema_check import assert_schema_is_current
from polska.orchestrator import run_startup_recovery, run_tick_cycle
from polska.runner import AgentRunner

logger = logging.getLogger("polska.main")


async def _tick_all_companies(
    session_factory,
    app_config: AppConfig,
    runner: AgentRunner,
    companies_dir: Path,
    workspace_root: Path,
) -> None:
    """Fired on every scheduler interval. The work itself lives in
    :func:`polska.orchestrator.run_tick_cycle`, shared with ``polska-cli tick``."""
    await run_tick_cycle(session_factory, app_config, runner, companies_dir, workspace_root)


async def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level)

    app_config = load_app_config(settings.config_path)
    engine = make_engine(settings.database_url, echo=settings.sql_echo)
    # Before anything else touches the database: a schema behind head must
    # stop this process outright, not run degraded until something happens to
    # query the difference. See schema_check.py for the incident this closes.
    assert_schema_is_current(engine)
    session_factory = make_session_factory(engine)

    run_startup_recovery(session_factory, app_config)

    adapter_registry = AdapterRegistry()
    budget_guard = BudgetGuard(app_config)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=budget_guard,
        adapter_registry=adapter_registry,
        agent_cli_path=settings.resolved_agent_cli_wrapper_path(),
    )

    scheduler = AsyncIOScheduler()
    if app_config.scheduler.enabled:
        # next_run_time takes a datetime or None; None means "add paused", not "no
        # immediate run". Omitting it entirely (leaving APScheduler's own
        # `undefined` default) is what lets the trigger compute the normal first
        # run; only run_on_start needs a value here, to force that first run now.
        extra_kwargs = {}
        if app_config.scheduler.run_on_start:
            extra_kwargs["next_run_time"] = dt.datetime.now(dt.UTC)

        scheduler.add_job(
            _tick_all_companies,
            trigger=IntervalTrigger(
                hours=app_config.scheduler.interval_hours,
                jitter=app_config.scheduler.jitter_seconds or None,
            ),
            args=(
                session_factory,
                app_config,
                runner,
                settings.companies_dir,
                settings.workspace_root,
            ),
            id="orchestrator-tick",
            coalesce=app_config.scheduler.coalesce,
            max_instances=1,
            **extra_kwargs,
        )
        scheduler.start()
        logger.info(
            "Scheduler started: every %.1fh, jitter %ds, coalesce=%s.",
            app_config.scheduler.interval_hours,
            app_config.scheduler.jitter_seconds,
            app_config.scheduler.coalesce,
        )
    else:
        logger.warning("scheduler.enabled is false; no ticks will fire. Running idle.")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
