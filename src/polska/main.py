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
from polska.config.company import discover_profiles, load_company_profile
from polska.config.settings import load_settings
from polska.db.base import make_engine, make_session_factory
from polska.orchestrator import run_company_tick, run_startup_recovery
from polska.runner import AgentRunner

logger = logging.getLogger("polska.main")


async def _tick_all_companies(
    session_factory,
    app_config: AppConfig,
    runner: AgentRunner,
    companies_dir: Path,
    workspace_root: Path,
) -> None:
    """Fired on every scheduler interval. Reloads profiles from disk each time, so
    an edited or newly added company YAML is picked up without a restart."""
    for path in discover_profiles(companies_dir):
        try:
            loaded = load_company_profile(path)
        except Exception:
            # A malformed profile must not take every other company's tick down
            # with it.
            logger.exception("Failed to load company profile %s; skipped this tick.", path)
            continue

        try:
            summary = await run_company_tick(
                session_factory, app_config, runner, loaded, workspace_root
            )
        except Exception:
            # One company's fatal error (e.g. the CLI itself being unreachable,
            # which run_company_tick deliberately re-raises) must not stop the
            # scheduler from at least trying the rest.
            logger.exception(
                "Tick for %s raised; other companies still ran this cycle.", loaded.profile.slug
            )
            continue

        logger.info(
            "%s: proposed=%d enqueued=%d deduped_out=%d requeued=%d dispatched=%d",
            loaded.profile.slug,
            summary.proposed,
            summary.enqueued,
            summary.deduped_out,
            summary.requeued,
            summary.dispatched,
        )


async def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level)

    app_config = load_app_config(settings.config_path)
    engine = make_engine(settings.database_url, echo=settings.sql_echo)
    session_factory = make_session_factory(engine)

    run_startup_recovery(session_factory, app_config)

    adapter_registry = AdapterRegistry()
    budget_guard = BudgetGuard(app_config)
    runner = AgentRunner(
        app_config=app_config, budget_guard=budget_guard, adapter_registry=adapter_registry
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
