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
from sqlalchemy import select
from sqlalchemy.orm import Session

from polska.budget import (
    BudgetExceeded,
    BudgetGuard,
    active_halt,
    clear_halt,
    reconcile_orphaned_runs,
)
from polska.config.appconfig import AppConfig
from polska.db.enums import ActivityKind, AgentName, BudgetScope, RunStatus
from polska.db.models import ActivityEvent, Company, Run


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
        agent=AgentName.ANALYST,
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
    """A config with a dollar ceiling low enough to hit in a couple of small runs.

    Every agent's own per-run override is cleared so every test in this file can
    assume the global figures above are what actually apply, regardless of which
    agents default.yaml happens to give their own override to."""
    return app_config.model_copy(
        update={
            "budget": app_config.budget.model_copy(
                update={
                    "max_usd_per_run": 1.0,
                    "max_usd_per_day": 2.5,
                    "max_usd_per_company": 100.0,
                }
            ),
            "agents": {
                name: agent.model_copy(update={"max_usd_per_run": None, "max_tokens_per_run": None})
                for name, agent in app_config.agents.items()
            },
        }
    )


# --------------------------------------------------------------------- reservation


async def test_a_reservation_within_budget_succeeds(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    guard = BudgetGuard(tight_config)
    reservation = await guard.reserve(
        session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
    )
    assert reservation.usd == tight_config.budget.max_usd_per_run
    assert reservation.company_id == company.id


async def test_reserving_past_the_daily_ceiling_is_refused(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    guard = BudgetGuard(tight_config)
    # Two reservations of $1 fit under $2.5. A third would bring the day to $3.
    await guard.reserve(
        session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
    )
    await guard.reserve(
        session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
    )
    with pytest.raises(BudgetExceeded) as excinfo:
        await guard.reserve(
            session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
        )
    assert excinfo.value.halt.limit_name == "max_usd_per_day"
    assert excinfo.value.halt.scope == BudgetScope.DAY


async def test_remaining_today_usd_reflects_committed_and_reserved_spend(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    """The planner's one source of truth: exactly what `reserve` would check
    against, not a separately derived figure."""
    guard = BudgetGuard(tight_config)
    assert guard.remaining_today_usd(session, company.id) == tight_config.budget.max_usd_per_day

    reservation = await guard.reserve(
        session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
    )
    assert guard.remaining_today_usd(session, company.id) == pytest.approx(
        tight_config.budget.max_usd_per_day - reservation.usd
    )

    guard.release(reservation)
    assert guard.remaining_today_usd(session, company.id) == tight_config.budget.max_usd_per_day


async def test_remaining_today_usd_can_go_negative_after_an_overshoot(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    """An open halt does not zero this out; it can be negative, which is the
    honest answer to "how much is left" when the day already went over."""
    guard = BudgetGuard(tight_config)
    await guard.reserve(
        session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
    )
    await guard.reserve(
        session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
    )
    with pytest.raises(BudgetExceeded):
        await guard.reserve(
            session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
        )
    # The refused reservation released itself (see reserve()'s `finally`-free
    # raise path -- nothing was ever added to _reservations for it), so this
    # reflects only the two that were actually granted.
    assert guard.remaining_today_usd(session, company.id) == pytest.approx(0.5)


async def test_a_refused_reservation_writes_a_budget_halt(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    """The stop is recorded before it takes effect. This is what proves that."""
    guard = BudgetGuard(tight_config)
    for _ in range(2):
        await guard.reserve(
            session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
        )
    with pytest.raises(BudgetExceeded):
        await guard.reserve(
            session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
        )

    halt = active_halt(session, company.id)
    assert halt is not None
    assert halt.is_active
    assert halt.overshoot > 0


async def test_release_frees_the_reservation_for_reuse(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    guard = BudgetGuard(tight_config)
    first = await guard.reserve(
        session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
    )
    second = await guard.reserve(
        session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
    )
    guard.release(first)
    guard.release(second)
    # Both released: a third reservation should not see any outstanding claim.
    third = await guard.reserve(
        session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
    )
    assert third.usd == 1.0


async def test_releasing_the_same_reservation_twice_is_harmless(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    guard = BudgetGuard(tight_config)
    reservation = await guard.reserve(
        session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
    )
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
        await guard.reserve(
            session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
        )


async def test_an_active_token_ceiling_halt_is_never_quoted_with_a_dollar_sign(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    """The exact incident this closes: an open run-scoped max_tokens_per_run
    halt, re-raised by reserve()'s active_halt() check on every subsequent
    dispatch attempt while it stays open, was rendered
    "$773927.0000 against a limit of 750000.00" -- a token count formatted as
    dollars, in the message that becomes Run.error and the activity feed."""
    from polska.budget import write_halt

    guard = BudgetGuard(tight_config)
    write_halt(
        session,
        company_id=company.id,
        scope=BudgetScope.RUN,
        limit_name="max_tokens_per_run",
        limit_value=750_000,
        observed_value=773_927,
        period_key=None,
        reason="manually injected for the test",
    )
    with pytest.raises(BudgetExceeded) as excinfo:
        await guard.reserve(
            session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
        )
    message = str(excinfo.value)
    assert "$773927" not in message
    assert "$750000" not in message
    assert "773927 tokens" in message
    assert "750000 tokens" in message


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

    reservation = await guard.reserve(
        session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
    )
    assert reservation is not None


# --------------------------------------------------------------------- clear_halt


async def test_clear_halt_records_who_and_writes_an_activity_event(
    session: Session, company: Company
) -> None:
    from polska.budget import write_halt

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
    assert halt.is_active

    cleared = clear_halt(session, halt, cleared_by="javier", note="topped up credit")
    assert not cleared.is_active
    assert cleared.cleared_by == "javier"
    assert cleared.cleared_at is not None

    events = list(session.execute(select(ActivityEvent)).scalars())
    resumed = [e for e in events if e.kind == ActivityKind.BUDGET_RESUMED]
    assert len(resumed) == 1
    assert resumed[0].detail["cleared_by"] == "javier"


async def test_clear_halt_refuses_an_already_cleared_halt(
    session: Session, company: Company
) -> None:
    """Clearing is a one-way, one-time act: re-clearing (or editing who cleared
    it) is not a thing this function does."""
    from polska.budget import write_halt

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
    clear_halt(session, halt, cleared_by="javier")
    with pytest.raises(ValueError, match="already cleared"):
        clear_halt(session, halt, cleared_by="someone_else")


async def test_a_run_scoped_halt_carries_the_run_that_caused_it(
    session: Session, company: Company
) -> None:
    """The dashboard shows the run a halt is about, not just its reason text."""
    from polska.budget import write_halt

    run = Run(
        company_id=company.id,
        agent=AgentName.ANALYST,
        model="claude-sonnet-5",
        status=RunStatus.INTERRUPTED,
        cost_usd=1.5,
    )
    session.add(run)
    session.flush()

    halt = write_halt(
        session,
        company_id=company.id,
        run_id=run.id,
        scope=BudgetScope.RUN,
        limit_name="mid_run_watchdog",
        limit_value=1.0,
        observed_value=1.5,
        period_key=None,
        reason=f"Run {run.id}: cut off mid-stream.",
    )
    assert halt.run_id == run.id
    assert halt.run is run
    assert run.halts == [halt]


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
        await guard.reserve(
            session, company_id=victim.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
        )


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
                guard.reserve(
                    sess,
                    company_id=company.id,
                    model="claude-sonnet-5",
                    agent_name=AgentName.SUPPORT,
                )
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
        await guard.reserve(
            session, company_id=company.id, model="claude-sonnet-5", agent_name=AgentName.SUPPORT
        )


# ------------------------------------------------------------------------- write-off


def test_writing_off_an_orphan_corrects_its_cost_and_marks_it_reconciled(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    from polska.budget import write_off_orphan

    orphan = _run(session, company, status=RunStatus.ORPHANED, cost_usd=1.0, fx_rate=0.79)

    written_off = write_off_orphan(
        session, orphan, actual_cost_usd=0.02, decided_by="javier", note="checked the console"
    )

    assert written_off.status == RunStatus.RECONCILED
    assert written_off.cost_usd == 0.02
    assert written_off.cost_gbp == pytest.approx(0.02 * 0.79)


def test_writing_off_an_orphan_leaves_an_audit_trail(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    from sqlalchemy import select

    from polska.budget import write_off_orphan
    from polska.db.enums import ActivityKind
    from polska.db.models import ActivityEvent

    orphan = _run(session, company, status=RunStatus.ORPHANED, cost_usd=1.0)
    write_off_orphan(session, orphan, actual_cost_usd=0.0, decided_by="javier", note="never billed")

    event = session.execute(
        select(ActivityEvent).where(ActivityEvent.kind == ActivityKind.ORPHAN_WRITTEN_OFF)
    ).scalar_one()
    assert event.detail["previous_cost_usd"] == 1.0
    assert event.detail["actual_cost_usd"] == 0.0
    assert event.detail["decided_by"] == "javier"
    assert event.detail["note"] == "never billed"


def test_writing_off_a_non_orphan_is_refused(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    from polska.budget import write_off_orphan

    run = _run(session, company, status=RunStatus.SUCCEEDED, cost_usd=0.5)
    with pytest.raises(ValueError, match="not orphaned"):
        write_off_orphan(session, run, actual_cost_usd=0.0, decided_by="javier")


def test_a_write_off_does_not_auto_clear_a_halt_it_caused(
    session: Session, tight_config: AppConfig, company: Company
) -> None:
    """Correcting the ledger downward is not, by itself, permission to resume."""
    from polska.budget import write_off_orphan

    for _ in range(3):
        _run(session, company, status=RunStatus.RUNNING, cost_usd=0.0, tokens=0)
    orphans = reconcile_orphaned_runs(session, tight_config)
    assert active_halt(session, company.id) is not None

    for orphan in orphans:
        write_off_orphan(session, orphan, actual_cost_usd=0.0, decided_by="javier")

    # Ledger is now clean, but the halt is still open: clearing it stays a
    # separate, deliberate act.
    assert active_halt(session, company.id) is not None


# ------------------------------------------------------------------------ task cost


def test_actual_usd_for_task_sums_across_every_run_the_task_has_had(
    session: Session, company: Company
) -> None:
    from polska.budget import actual_usd_for_task
    from polska.db.enums import GoalStatus, TaskType
    from polska.db.models import Goal, Task

    goal = Goal(
        company_id=company.id,
        key="test-goal",
        title="Test goal",
        metric="x",
        target_value=1,
        status=GoalStatus.ACTIVE,
    )
    session.add(goal)
    session.flush()
    task = Task(company_id=company.id, goal_id=goal.id, type=TaskType.RESEARCH, title="A task")
    session.add(task)
    session.flush()

    for cost in (0.5, 1.25):
        run = _run(session, company, cost_usd=cost)
        run.task_id = task.id
    session.commit()

    assert actual_usd_for_task(session, task.id) == pytest.approx(1.75)


def test_actual_usd_for_task_is_zero_for_a_task_with_no_runs(session: Session) -> None:
    from polska.budget import actual_usd_for_task

    assert actual_usd_for_task(session, 999) == 0.0
