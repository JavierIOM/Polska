"""The budget guard: reserve before you spend, and price it in dollars.

This is the fix for the concurrency gap Javier flagged in review: two dispatches
reading the same stale total before either had written its cost could together blow
past a ceiling neither one alone would have crossed. These tests exist to prove that
specific race cannot happen any more, not just that the arithmetic is right.

The ledger and every ceiling here are dollars, not tokens: tokens are not a fungible
unit across models (an Opus token and a Haiku token do not cost the same), so a
cross-model raw token sum would make a day-or-company ceiling meaningless the moment
two different models are in play, which they are from the shipped config onwards.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.orm import Session

from polska.budget import BudgetExceeded, BudgetGuard, active_halt, reconcile_orphaned_runs
from polska.config.appconfig import AppConfig
from polska.db.enums import BudgetScope, RunStatus
from polska.db.models import Company, Run


def _company(session: Session, **overrides: object) -> Company:
    record = Company(
        slug=overrides.pop("slug", "budget-co"),
        name="Budget Co",
        profile_path="companies/budget-co.yaml",
        profile_hash="0" * 64,
        profile={},
        idea="Testing budgets.",
    )
    for key, value in overrides.items():
        setattr(record, key, value)
    session.add(record)
    session.commit()
    return record


def _run(
    session: Session,
    company: Company,
    *,
    tokens: int = 0,
    cost_usd: float = 0.0,
    **overrides: object,
) -> Run:
    record = Run(
        company_id=company.id,
        agent="analyst",
        model="claude-sonnet-5",
        input_tokens=tokens,
        cost_usd=cost_usd,
        status=RunStatus.SUCCEEDED,
    )
    for key, value in overrides.items():
        setattr(record, key, value)
    session.add(record)
    session.commit()
    return record


@pytest.fixture
def tight_config(app_config: AppConfig) -> AppConfig:
    """A config with a dollar ceiling low enough to hit in a couple of small runs."""
    return app_config.model_copy(
        update={
            "budget": app_config.budget.model_copy(
                update={
                    "max_usd_per_run": 1.0,
                    "max_usd_per_day": 2.5,
                    "max_usd_per_company": 100.0,
                }
            )
        }
    )


# --------------------------------------------------------------------- reservation


async def test_a_reservation_within_budget_succeeds(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    guard = BudgetGuard(tight_config)
    reservation = await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
    assert reservation.usd == tight_config.budget.max_usd_per_run
    assert reservation.company_id == company.id


async def test_reserving_past_the_daily_ceiling_is_refused(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    guard = BudgetGuard(tight_config)
    # Two reservations of $1 fit under $2.5. A third would bring the day to $3.
    await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
    await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
    with pytest.raises(BudgetExceeded) as excinfo:
        await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
    assert excinfo.value.halt.limit_name == "max_usd_per_day"
    assert excinfo.value.halt.scope == BudgetScope.DAY


async def test_a_refused_reservation_writes_a_budget_halt(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    """The stop is recorded before it takes effect. This is what proves that."""
    guard = BudgetGuard(tight_config)
    for _ in range(2):
        await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
    with pytest.raises(BudgetExceeded):
        await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")

    halt = active_halt(session, company.id)
    assert halt is not None
    assert halt.is_active
    assert halt.overshoot > 0


async def test_release_frees_the_reservation_for_reuse(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    guard = BudgetGuard(tight_config)
    first = await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
    second = await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
    guard.release(first)
    guard.release(second)
    # Both released: a third reservation should not see any outstanding claim.
    third = await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
    assert third.usd == 1.0


async def test_releasing_the_same_reservation_twice_is_harmless(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    guard = BudgetGuard(tight_config)
    reservation = await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
    guard.release(reservation)
    guard.release(reservation)  # must not raise


async def test_an_active_halt_blocks_every_new_reservation_regardless_of_sums(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    """Even with room left under every ceiling, an uncleared halt stops everything.
    Clearing one is a deliberate human act, never something the guard talks itself
    back into."""
    from polska.budget import write_halt

    guard = BudgetGuard(tight_config)
    write_halt(
        session,
        company_id=company.id,
        scope=BudgetScope.COMPANY,
        limit_name="max_usd_per_company",
        limit_value=1.0,
        observed_value=2.0,
        period_key=None,
        reason="manually injected for the test",
    )
    with pytest.raises(BudgetExceeded):
        await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")


async def test_a_cleared_halt_no_longer_blocks(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    from polska.budget import write_halt

    guard = BudgetGuard(tight_config)
    halt = write_halt(
        session,
        company_id=company.id,
        scope=BudgetScope.COMPANY,
        limit_name="max_usd_per_company",
        limit_value=1.0,
        observed_value=2.0,
        period_key=None,
        reason="manually injected for the test",
    )
    halt.cleared_at = halt.created_at
    halt.cleared_by = "javier"
    session.commit()

    reservation = await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
    assert reservation is not None


async def test_a_global_halt_blocks_a_company_that_never_tripped_it(
    session: Session, tight_config: AppConfig
) -> None:
    from polska.budget import write_halt

    guard = BudgetGuard(tight_config)
    other = _company(session, slug="other-co")
    victim = _company(session, slug="victim-co")

    write_halt(
        session,
        company_id=None,  # global
        scope=BudgetScope.COMPANY,
        limit_name="max_usd_per_company",
        limit_value=1.0,
        observed_value=2.0,
        period_key=None,
        reason=f"tripped by {other.slug}",
    )
    with pytest.raises(BudgetExceeded):
        await guard.reserve(session, company_id=victim.id, model="claude-sonnet-5")


# --------------------------------------------------------------- the actual race


async def test_concurrent_reservations_cannot_together_exceed_the_ceiling(
    session_factory, tight_config: AppConfig, company: Company
) -> None:
    """The scenario from review: several dispatches starting in the same window,
    each checking the ledger before any of them has written a Run row.

    Without the lock and the in-memory reservation, every one of these would read
    the same near-empty ledger and all would pass. With it, only as many succeed as
    actually fit, and the rest see the ceiling and stop."""
    guard = BudgetGuard(tight_config)
    # max_usd_per_day is 2.5, each reservation claims $1: exactly two must fit,
    # every other concurrent attempt must be refused.
    sessions = [session_factory() for _ in range(5)]
    try:
        results = await asyncio.gather(
            *(
                guard.reserve(sess, company_id=company.id, model="claude-sonnet-5")
                for sess in sessions
            ),
            return_exceptions=True,
        )
    finally:
        for sess in sessions:
            sess.close()

    granted = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, BudgetExceeded)]
    assert len(granted) == 2
    assert len(refused) == 3
    assert sum(r.usd for r in granted) <= tight_config.budget.max_usd_per_day


# ------------------------------------------------------------------------ overshoot


def test_a_run_within_its_reservation_does_not_trip_the_overshoot_check(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    guard = BudgetGuard(tight_config)
    run = _run(session, company, cost_usd=0.5, tokens=100)
    assert guard.check_run_did_not_overshoot(session, company_id=company.id, run=run) == []


def test_a_run_over_its_dollar_reservation_trips_a_run_scoped_halt(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    """The reservation is meant to be a hard upper bound. A run exceeding it means
    max_budget_usd did not hold, which is worth a loud halt, not a shrug."""
    guard = BudgetGuard(tight_config)
    run = _run(session, company, cost_usd=5.0, tokens=100)
    halts = guard.check_run_did_not_overshoot(session, company_id=company.id, run=run)
    assert len(halts) == 1
    assert halts[0].scope == BudgetScope.RUN
    assert halts[0].limit_name == "max_usd_per_run"


def test_a_run_over_its_token_safety_net_trips_a_separate_halt(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    """Token overshoot is independent of cost overshoot: a cheap model generating
    an absurd number of tokens should be caught even if it stayed under budget."""
    guard = BudgetGuard(tight_config)
    run = _run(session, company, cost_usd=0.1, tokens=10_000_000)
    halts = guard.check_run_did_not_overshoot(session, company_id=company.id, run=run)
    assert len(halts) == 1
    assert halts[0].limit_name == "max_tokens_per_run"


def test_a_run_can_trip_both_overshoot_checks_at_once(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    guard = BudgetGuard(tight_config)
    run = _run(session, company, cost_usd=50.0, tokens=10_000_000)
    halts = guard.check_run_did_not_overshoot(session, company_id=company.id, run=run)
    names = {h.limit_name for h in halts}
    assert names == {"max_usd_per_run", "max_tokens_per_run"}


# --------------------------------------------------------------- orphan recovery


def test_a_running_row_left_from_a_previous_process_is_recovered(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    """Simulates the crash case: a Run was written in `running` state before the
    SDK call, and the process died before it could ever be finalised. The next
    process start must not treat that as zero spend."""
    orphan = _run(session, company, status=RunStatus.RUNNING, cost_usd=0.0, tokens=0)

    recovered = reconcile_orphaned_runs(session, tight_config)

    assert len(recovered) == 1
    assert recovered[0].id == orphan.id
    session.refresh(orphan)
    assert orphan.status == RunStatus.ORPHANED
    # Priced at the reservation's worst case, not left at zero: a crash must never
    # be recorded as cheaper than it might really have been.
    assert orphan.cost_usd == tight_config.budget.max_usd_per_run
    assert orphan.finished_at is not None
    assert "interrupted" in orphan.error.lower()


def test_a_clean_process_start_with_no_orphans_does_nothing(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    _run(session, company, status=RunStatus.SUCCEEDED, cost_usd=0.5, tokens=100)
    assert reconcile_orphaned_runs(session, tight_config) == []


def test_recovering_orphans_that_push_over_the_ceiling_writes_a_halt(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    """This is the scenario from review: repeated crashes could take spend well
    past a ceiling while the ledger still looked healthy. After recovery it must
    not look healthy any more, and a halt must exist to stop further work."""
    # Three orphans at the $1 worst case each exceed the $2.5 daily ceiling once
    # priced in.
    for _ in range(3):
        _run(session, company, status=RunStatus.RUNNING, cost_usd=0.0, tokens=0)

    reconcile_orphaned_runs(session, tight_config)

    halt = active_halt(session, company.id)
    assert halt is not None
    assert halt.limit_name == "max_usd_per_day"


async def test_a_reservation_is_blocked_after_orphan_recovery_trips_a_halt(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    for _ in range(3):
        _run(session, company, status=RunStatus.RUNNING, cost_usd=0.0, tokens=0)
    reconcile_orphaned_runs(session, tight_config)

    guard = BudgetGuard(tight_config)
    with pytest.raises(BudgetExceeded):
        await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
