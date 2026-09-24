"""The budget guard: reserve before you spend.

This is the fix for the concurrency gap Javier flagged in review: two dispatches
reading the same stale total before either had written its cost could together blow
past a ceiling neither one alone would have crossed. These tests exist to prove that
specific race cannot happen any more, not just that the arithmetic is right.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.orm import Session

from polska.budget import BudgetExceeded, BudgetGuard, active_halt
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


def _run(session: Session, company: Company, *, tokens: int, cost_usd: float = 0.0) -> Run:
    record = Run(
        company_id=company.id,
        agent="analyst",
        model="claude-sonnet-5",
        input_tokens=tokens,
        cost_usd=cost_usd,
        status=RunStatus.SUCCEEDED,
    )
    session.add(record)
    session.commit()
    return record


@pytest.fixture
def tight_config(app_config: AppConfig) -> AppConfig:
    """A config with a ceiling low enough to hit in a couple of small runs."""
    return app_config.model_copy(
        update={
            "budget": app_config.budget.model_copy(
                update={
                    "max_tokens_per_run": 100,
                    "max_tokens_per_day": 250,
                    "max_tokens_per_company": 1000,
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
    assert reservation.tokens == tight_config.budget.max_tokens_per_run
    assert reservation.company_id == company.id


async def test_reserving_past_the_daily_ceiling_is_refused(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    guard = BudgetGuard(tight_config)
    # Two reservations of 100 fit under 250. A third would bring the day to 300.
    await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
    await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
    with pytest.raises(BudgetExceeded) as excinfo:
        await guard.reserve(session, company_id=company.id, model="claude-sonnet-5")
    assert excinfo.value.halt.limit_name == "max_tokens_per_day"
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
    assert third.tokens == 100


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
    guard = BudgetGuard(tight_config)
    guard._write_halt(  # noqa: SLF001 - simulating an operator-visible halt directly
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
    guard = BudgetGuard(tight_config)
    halt = guard._write_halt(  # noqa: SLF001
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
    guard = BudgetGuard(tight_config)
    other = _company(session, slug="other-co")
    victim = _company(session, slug="victim-co")

    guard._write_halt(  # noqa: SLF001
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
    # max_tokens_per_day is 250, each reservation claims 100: exactly two must fit,
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
    assert sum(r.tokens for r in granted) <= tight_config.budget.max_tokens_per_day


# ------------------------------------------------------------------------ overshoot


def test_a_run_within_its_reservation_does_not_trip_the_overshoot_check(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    guard = BudgetGuard(tight_config)
    run = _run(session, company, tokens=50)
    assert guard.check_run_did_not_overshoot(session, company_id=company.id, run=run) is None


def test_a_run_over_its_reservation_trips_a_run_scoped_halt(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    """The reservation is meant to be a hard upper bound. A run exceeding it means
    the estimate was wrong, and that is worth a loud halt, not a shrug."""
    guard = BudgetGuard(tight_config)
    run = _run(session, company, tokens=500)
    halt = guard.check_run_did_not_overshoot(session, company_id=company.id, run=run)
    assert halt is not None
    assert halt.scope == BudgetScope.RUN
    assert halt.limit_name == "max_tokens_per_run"


# --------------------------------------------------------------------------- USD


async def test_a_dollar_ceiling_blocks_independently_of_the_token_ceiling(
    session: Session, app_config: AppConfig, company: Company
) -> None:
    """A cheap-per-token model could stay under every token ceiling while still
    being expensive, if the dollar ceiling is tighter. Both must be enforced."""
    config = app_config.model_copy(
        update={
            "budget": app_config.budget.model_copy(
                update={"max_usd_per_day": 0.0001, "max_usd_per_company": 0.0001}
            )
        }
    )
    guard = BudgetGuard(config)
    with pytest.raises(BudgetExceeded) as excinfo:
        await guard.reserve(session, company_id=company.id, model="claude-opus-5")
    assert "usd" in excinfo.value.halt.limit_name
