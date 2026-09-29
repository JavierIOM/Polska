"""Run N real ticks against a company profile and report exactly what happened.

For watching what the orchestrator actually does against a real company, not what
the tests say it can do. Prints the activity feed for each tick, then a per-run
cost breakdown by agent, then the state and dedup note of every task so far.

Does not touch ``integrations.force_dry_run``: whatever config/default.yaml says,
this respects. Leave it ``true`` unless you specifically mean to let an adapter
perform a real external effect.

Usage, from the repo root, with the package installed editable (`pip install -e .`,
already true for anyone who has run the test suite):

    .venv\\Scripts\\python.exe scripts\\observe_ticks.py <company-slug> [num-ticks] \\
        [--max-concurrent N] [--stop-if-over USD]

``--max-concurrent`` overrides ``limits.max_concurrent_tasks`` for this invocation
only; the config file on disk is untouched. ``--stop-if-over`` checks cumulative
spend after each tick and stops before starting the next one if it has already been
exceeded, so a session with a real dollar cap can't blow through it unattended
between ticks.

Runs against the real database named in settings (``db/polska.db`` by default),
not a throwaway one: a tick's history matters to the next tick's dedup, so this is
meant to accumulate state across runs the same way the real scheduler would.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select

from polska.adapters.registry import AdapterRegistry
from polska.budget import BudgetGuard
from polska.config.appconfig import load_app_config
from polska.config.company import load_company_profile
from polska.config.settings import load_settings
from polska.db.base import make_engine, make_session_factory
from polska.db.models import ActivityEvent, Run, Task
from polska.orchestrator import run_company_tick, run_startup_recovery
from polska.runner import AgentRunner

REPO_ROOT = Path(__file__).resolve().parents[1]


async def main(
    slug: str,
    num_ticks: int,
    *,
    max_concurrent: int | None = None,
    stop_if_over: float | None = None,
) -> None:
    settings = load_settings()
    app_config = load_app_config(settings.config_path)
    if max_concurrent is not None:
        app_config = app_config.model_copy(
            update={
                "limits": app_config.limits.model_copy(
                    update={"max_concurrent_tasks": max_concurrent}
                )
            }
        )
        print(f"max_concurrent_tasks overridden to {max_concurrent} for this run only")
    print(f"force_dry_run = {app_config.integrations.force_dry_run}")
    if not app_config.integrations.force_dry_run:
        print("WARNING: force_dry_run is false. A real adapter call could have a real effect.")

    engine = make_engine(settings.database_url, echo=settings.sql_echo)
    session_factory = make_session_factory(engine)
    run_startup_recovery(session_factory, app_config)

    registry = AdapterRegistry()
    guard = BudgetGuard(app_config)
    runner = AgentRunner(app_config=app_config, budget_guard=guard, adapter_registry=registry)

    loaded = load_company_profile(REPO_ROOT / "companies" / f"{slug}.yaml")

    grand_total = 0.0
    for tick_num in range(1, num_ticks + 1):
        with session_factory() as session:
            run_watermark = max(session.execute(select(Run.id)).scalars().all(), default=0)
            event_watermark = max(
                session.execute(select(ActivityEvent.id)).scalars().all(), default=0
            )

        print(f"\n{'=' * 70}\nTICK {tick_num}\n{'=' * 70}")
        summary = await run_company_tick(
            session_factory, app_config, runner, loaded, settings.workspace_root
        )
        print(
            f"proposed={summary.proposed} deduped_out={summary.deduped_out} "
            f"skipped_daily_cap={summary.skipped_daily_cap} enqueued={summary.enqueued} "
            f"requeued={summary.requeued} dispatched={summary.dispatched}"
        )

        with session_factory() as session:
            print("\n-- activity feed this tick --")
            events = session.execute(
                select(ActivityEvent)
                .where(ActivityEvent.id > event_watermark)
                .order_by(ActivityEvent.id)
            ).scalars()
            for event in events:
                line = f"  [{event.kind.value}] {event.summary}"
                if event.detail:
                    line += f"  {event.detail}"
                if event.error:
                    line += f"  ERROR: {event.error}"
                print(line)

            print("\n-- runs this tick, cost by agent --")
            runs = list(
                session.execute(
                    select(Run).where(Run.id > run_watermark).order_by(Run.id)
                ).scalars()
            )
            tick_total = 0.0
            by_agent: dict[str, float] = {}
            for run in runs:
                by_agent[run.agent.value] = by_agent.get(run.agent.value, 0.0) + run.cost_usd
                tick_total += run.cost_usd
                print(
                    f"  {run.agent.value:12} {run.model:20} {run.status.value:16} "
                    f"${run.cost_usd:.5f}  in={run.input_tokens} out={run.output_tokens} "
                    f"cache_read={run.cache_read_tokens} cache_write={run.cache_creation_tokens}"
                )
            for agent, cost in by_agent.items():
                print(f"    {agent:12} ${cost:.5f}")
            print(f"  tick {tick_num} total: ${tick_total:.5f}")
            grand_total += tick_total

            print("\n-- all tasks so far, with state and dedup note --")
            for task in session.execute(select(Task).order_by(Task.id)).scalars():
                print(
                    f"  #{task.id} [{task.state.value}] attempts={task.attempts} "
                    f"prio={task.priority} {task.title!r}"
                )
                if task.dedup_note:
                    print(f"      dedup: {task.dedup_note}")

        if stop_if_over is not None and grand_total > stop_if_over:
            print(
                f"\n{'=' * 70}\nSTOPPED: spent ${grand_total:.5f}, over the ${stop_if_over:.2f} "
                f"cap, after tick {tick_num}/{num_ticks}. Not starting another tick.\n{'=' * 70}"
            )
            return

    print(f"\n{'=' * 70}\nDONE: {num_ticks} tick(s), ${grand_total:.5f} total\n{'=' * 70}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("company_slug")
    parser.add_argument("num_ticks", type=int, nargs="?", default=3)
    parser.add_argument("--max-concurrent", type=int, default=None)
    parser.add_argument("--stop-if-over", type=float, default=None)
    args = parser.parse_args()
    asyncio.run(
        main(
            args.company_slug,
            args.num_ticks,
            max_concurrent=args.max_concurrent,
            stop_if_over=args.stop_if_over,
        )
    )
