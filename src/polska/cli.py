"""Operator commands run by hand, not by the scheduler.

    python -m polska.cli init-auth   # generate the dashboard's admin credentials
    python -m polska.cli tick [slug] # trigger one planning/dispatch cycle now

Both exist because the scheduler deliberately never does either on its own: it
will not pick a password, and (see main.py / IntervalTrigger) it will not tick
itself the moment a fresh container boots. Both are things only a human decides.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import secrets
import sys

from polska.adapters.registry import AdapterRegistry
from polska.budget import BudgetGuard
from polska.config.appconfig import load_app_config
from polska.config.settings import Settings, load_settings
from polska.dashboard.security import hash_password
from polska.db.base import make_engine, make_session_factory
from polska.db.enums import RunStatus
from polska.db.schema_check import assert_schema_is_current
from polska.orchestrator import UnknownCompanyError, run_startup_recovery, run_tick_cycle
from polska.runner import AgentRunner
from polska.ticklock import tick_lock, tick_lock_path


def _init_auth() -> None:
    """Prompt for a password, print the hash and a fresh session secret.

    Never accepts the password as a command-line argument: that would put it
    in shell history and the process list on a shared box. getpass is the one
    thing that does not echo it back or log it anywhere.
    """
    password = getpass.getpass("Dashboard admin password: ")
    confirm = getpass.getpass("Confirm: ")
    if password != confirm:
        print("Passwords did not match. Nothing generated.", file=sys.stderr)
        raise SystemExit(1)
    if len(password) < 12:
        print(
            "That's under 12 characters. This is the only thing standing between "
            "the internet and an approve button while the dashboard is public; "
            "use something longer.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    password_hash = hash_password(password)
    session_secret = secrets.token_urlsafe(32)

    # Single-quoted, deliberately: an Argon2 hash contains literal $ characters
    # ($argon2id$v=19$m=...$salt$hash), and Docker Compose's .env parsing
    # interpolates $ as a variable reference in an unquoted or double-quoted
    # value (confirmed against Compose's own env_file docs), silently mangling
    # the hash into whatever the referenced variable happens to expand to
    # (usually nothing) -- a login that then cannot work, with no error
    # anywhere to say why. Single quotes make Compose (and python-dotenv, for
    # a non-Docker .env) take the value literally instead; verified against
    # both. The session secret never contains $, but it costs nothing to quote
    # it the same way for consistency.
    print("\nAdd these to your .env (never to a tracked file). Keep the quotes: they")
    print("stop Docker Compose from misreading the $ characters in the hash as")
    print("variable references, which would silently corrupt it.\n")
    print(f"POLSKA_ADMIN_PASSWORD_HASH='{password_hash}'")
    print(f"POLSKA_SESSION_SECRET='{session_secret}'")
    print("\nAfter editing .env, verify it landed correctly rather than assuming it did:")
    print("  docker compose run --rm scheduler env | grep POLSKA_ADMIN_PASSWORD_HASH")
    print("and compare it character-for-character against what was printed above.")


async def _tick(slug: str | None) -> None:
    """Run exactly one planning/dispatch cycle now, for one company or all of
    them, then exit. The same function the scheduler calls on its own interval
    (``run_tick_cycle``); this is the manual override for "not 24 hours from now".
    Exits 1 if another tick is running, a company failed or was halted, or a planner
    run did not succeed."""
    settings = load_settings()
    logging.basicConfig(level=settings.log_level)
    with tick_lock(tick_lock_path(settings.workspace_root)) as acquired:
        if not acquired:
            print("Another tick is already running; not starting a second.", file=sys.stderr)
            raise SystemExit(1)
        await _tick_holding_lock(settings, slug)


async def _tick_holding_lock(settings: Settings, slug: str | None) -> None:
    app_config = load_app_config(settings.config_path)
    engine = make_engine(settings.database_url, echo=settings.sql_echo)
    assert_schema_is_current(engine)
    session_factory = make_session_factory(engine)

    # Safe only because the tick lock is held: any run or task still marked running
    # now really was left behind by a process that died.
    run_startup_recovery(session_factory, app_config)

    registry = AdapterRegistry()
    guard = BudgetGuard(app_config)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=guard,
        adapter_registry=registry,
        agent_cli_path=settings.resolved_agent_cli_wrapper_path(),
    )

    try:
        result = await run_tick_cycle(
            session_factory,
            app_config,
            runner,
            settings.companies_dir,
            settings.workspace_root,
            only_slug=slug,
        )
    except UnknownCompanyError:
        print(f"No company profile named {slug!r} in {settings.companies_dir}.", file=sys.stderr)
        raise SystemExit(1) from None

    for company, summary in result.summaries.items():
        if summary.halted_by:
            print(
                f"{company}: stopped by {summary.halted_by}, nothing planned or dispatched. "
                "Clear it on the dashboard's budget page to resume."
            )
            continue
        status = summary.planner_status.value if summary.planner_status else "n/a"
        print(
            f"{company}: planner={status} proposed={summary.proposed} "
            f"enqueued={summary.enqueued} deduped_out={summary.deduped_out} "
            f"requeued={summary.requeued} dispatched={summary.dispatched}"
        )
        if summary.planner_status not in (None, RunStatus.SUCCEEDED):
            print(f"  planner error: {(summary.planner_error or '')[:400]}", file=sys.stderr)

    if result.failed or result.planner_problems or result.halted:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="polska-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "init-auth", help="Generate the dashboard admin password hash and session secret."
    )

    tick_parser = subparsers.add_parser(
        "tick", help="Run one planning/dispatch cycle now, instead of waiting for the scheduler."
    )
    tick_parser.add_argument(
        "slug", nargs="?", default=None, help="One company's slug. Omit to tick every company."
    )

    args = parser.parse_args()

    if args.command == "init-auth":
        _init_auth()
    elif args.command == "tick":
        asyncio.run(_tick(args.slug))


if __name__ == "__main__":
    main()
