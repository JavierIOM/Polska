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
import secrets
import sys

from polska.adapters.registry import AdapterRegistry
from polska.budget import BudgetGuard
from polska.config.appconfig import load_app_config
from polska.config.company import discover_profiles, load_company_profile
from polska.config.settings import load_settings
from polska.dashboard.security import hash_password
from polska.db.base import make_engine, make_session_factory
from polska.db.schema_check import assert_schema_is_current
from polska.orchestrator import run_company_tick, run_startup_recovery
from polska.runner import AgentRunner


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
    them, then exit. The same call the scheduler itself makes on its own
    interval; this is the manual override for "not 24 hours from now"."""
    settings = load_settings()
    app_config = load_app_config(settings.config_path)
    engine = make_engine(settings.database_url, echo=settings.sql_echo)
    assert_schema_is_current(engine)
    session_factory = make_session_factory(engine)

    run_startup_recovery(session_factory, app_config)

    registry = AdapterRegistry()
    guard = BudgetGuard(app_config)
    runner = AgentRunner(
        app_config=app_config,
        budget_guard=guard,
        adapter_registry=registry,
        agent_cli_path=settings.resolved_agent_cli_wrapper_path(),
    )

    paths = list(discover_profiles(settings.companies_dir))
    if slug:
        paths = [p for p in paths if p.stem == slug]
        if not paths:
            print(
                f"No company profile named {slug!r} in {settings.companies_dir}.", file=sys.stderr
            )
            raise SystemExit(1)

    for path in paths:
        loaded = load_company_profile(path)
        summary = await run_company_tick(
            session_factory, app_config, runner, loaded, settings.workspace_root
        )
        print(
            f"{loaded.profile.slug}: proposed={summary.proposed} enqueued={summary.enqueued} "
            f"deduped_out={summary.deduped_out} requeued={summary.requeued} "
            f"dispatched={summary.dispatched}"
        )


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
