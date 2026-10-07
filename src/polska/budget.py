"""The budget guard: reserve before you spend, not just sum after you did.

Summing actual spend from ``runs`` is the ledger, and it is the right source of
truth: cost is never recorded twice and never guessed at. But a ledger read is a
snapshot, and if two tasks each read it before either has written to it, both can
pass a check that only one of them should have passed. With ``max_concurrent_tasks``
at 2 or more, that race is not hypothetical.

The fix is a reservation held for the lifetime of one run, checked against actual
spend plus every other reservation currently outstanding, atomically. Nothing here
persists a reservation to the database: this process is single-process by design (one
scheduler, one semaphore for concurrency), so an in-memory counter guarded by an
``asyncio.Lock`` is enough.

The ledger itself is denominated in dollars, not tokens. Tokens are not a fungible
unit across models, and this project runs several: summing raw token counts across an
Opus run and a Haiku run would make a day-or-company ceiling meaningless the moment
two different models are in play, which they are from the shipped config onwards.
Dollars, computed from the same pricing table that already prices every run, are the
only unit that adds up correctly.

Two things a naive reading of "reserve, then release" gets wrong, both fixed here:

- A crash mid-run does not just lose an in-memory reservation, it loses a real,
  already-billed API call with no ``Run`` row to show it, which is real spend a
  sum-of-``runs`` ledger cannot see. :func:`reconcile_orphaned_runs` is the fix: every
  run is written to the database in ``running`` state before the SDK is ever called,
  so a crash leaves a row behind, not a silent gap, and the next process start prices
  it at its reservation's worst case rather than assuming it cost nothing.
- A dispatch that fails a check is recorded as loudly as one that succeeds: a
  :class:`BudgetHalt` row is committed before :class:`BudgetExceeded` is raised.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from polska.activity import log
from polska.config.appconfig import AppConfig, BudgetConfig
from polska.db.enums import ActivityKind, AgentName, BudgetScope, RunStatus
from polska.db.models import BudgetHalt, Run
from polska.db.types import utcday, utcnow


@dataclass(frozen=True, slots=True)
class Reservation:
    """A held claim against the budget for one in-flight run.

    ``usd`` is always ``max_usd_per_run`` in full, not an estimate of what the run
    will likely cost. That is what makes the guard exact rather than merely
    usually-right, and it is also the figure ``max_budget_usd`` asks the SDK itself
    to hold the run to, so the two enforcement mechanisms agree on the same number.
    """

    id: str
    company_id: int
    model: str
    usd: float


class BudgetExceeded(Exception):
    """Raised when a reservation would cross a ceiling, or one already has.

    Carries the :class:`BudgetHalt` that was written (or that was already open), so
    the caller can report exactly why without re-deriving it.
    """

    def __init__(self, halt: BudgetHalt) -> None:
        self.halt = halt
        if halt.is_token_denominated:
            figures = (
                f"{halt.observed_value:.0f} tokens against a limit of {halt.limit_value:.0f} tokens"
            )
        else:
            figures = f"${halt.observed_value:.4f} against a limit of ${halt.limit_value:.2f}"
        super().__init__(
            f"{halt.scope.value} ceiling '{halt.limit_name}' would be crossed: "
            f"{figures}. {halt.reason}"
        )


def _actual_usd(session: Session, company_id: int, *, since_day: str | None = None) -> float:
    """Real dollar spend from committed ``Run`` rows. The only source of truth.

    ``cost_usd`` already prices input, output and cache tokens at each run's own
    model's rate (or the SDK's own reported cost, when it gave one), so this sum is
    correct across a mix of models in a way a raw token count could never be.
    """
    total_expr = func.coalesce(func.sum(Run.cost_usd), 0.0)
    stmt = select(total_expr).where(Run.company_id == company_id)
    if since_day is not None:
        stmt = stmt.where(func.strftime("%Y-%m-%d", Run.started_at) == since_day)
    return float(session.execute(stmt).scalar_one())


def actual_usd_for_task(session: Session, task_id: int) -> float:
    """Real dollar spend across every run a single task has had.

    Queried directly rather than through ``Task.runs``: the session may already
    have that relationship loaded from earlier in the same request, and with
    ``expire_on_commit=False`` a freshly committed run is not guaranteed to appear
    in an already-cached collection. A direct query is never stale.
    """
    total_expr = func.coalesce(func.sum(Run.cost_usd), 0.0)
    stmt = select(total_expr).where(Run.task_id == task_id)
    return float(session.execute(stmt).scalar_one())


def active_halt(session: Session, company_id: int) -> BudgetHalt | None:
    """The uncleared halt blocking this company, if there is one.

    A global halt (``company_id`` null) blocks every company. Checked first and
    unconditionally: an open halt means a human has not yet decided it is safe to
    resume, and that decision is never made by the guard itself.
    """
    stmt = (
        select(BudgetHalt)
        .where(BudgetHalt.cleared_at.is_(None))
        .where((BudgetHalt.company_id == company_id) | (BudgetHalt.company_id.is_(None)))
        .order_by(BudgetHalt.created_at.asc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def write_halt(
    session: Session,
    *,
    company_id: int | None,
    scope: BudgetScope,
    limit_name: str,
    limit_value: float,
    observed_value: float,
    period_key: str | None,
    reason: str,
    run_id: int | None = None,
) -> BudgetHalt:
    """Record a crossed ceiling. Committed immediately: the stop is logged before
    it takes effect, the same rule the approval gate follows for external effects.

    ``run_id`` is the specific run that tripped this, for a RUN-scoped halt; left
    unset for a DAY/COMPANY-scoped one, which is never about a single run. This
    is what lets the dashboard show the run that caused a halt, not just its
    reason text.
    """
    halt = BudgetHalt(
        company_id=company_id,
        run_id=run_id,
        scope=scope,
        limit_name=limit_name,
        limit_value=limit_value,
        observed_value=observed_value,
        period_key=period_key,
        reason=reason,
        created_at=utcnow(),
    )
    session.add(halt)
    session.commit()
    return halt


def clear_halt(
    session: Session, halt: BudgetHalt, *, cleared_by: str, note: str = ""
) -> BudgetHalt:
    """Resume a company (or, for a global halt, every company) after a human has
    decided it is safe. The one function the dashboard's clear button calls.

    Deliberately the only way a :class:`BudgetHalt` ever stops blocking: nothing
    in :class:`BudgetGuard` calls this itself, by design, so the guard can never
    talk itself back into spending. ``cleared_by`` is required, never defaulted,
    so every resume is a stated decision with a name attached, the same
    discipline :func:`write_off_orphan` applies to correcting a cost figure.
    """
    if not halt.is_active:
        raise ValueError(
            f"BudgetHalt {halt.id} was already cleared at {halt.cleared_at} by "
            f"{halt.cleared_by!r}. clear_halt only resumes an open halt; it is not "
            "a way to edit or re-clear one that is already closed."
        )

    halt.cleared_at = utcnow()
    halt.cleared_by = cleared_by

    log(
        session,
        company_id=halt.company_id,
        kind=ActivityKind.BUDGET_RESUMED,
        summary=(f"{halt.scope.value} ceiling '{halt.limit_name}' halt cleared by {cleared_by}"),
        detail={
            "halt_id": halt.id,
            "limit_name": halt.limit_name,
            "limit_value": halt.limit_value,
            "observed_value": halt.observed_value,
            "cleared_by": cleared_by,
            "note": note,
        },
    )
    session.commit()
    return halt


class BudgetGuard:
    """Reserves budget before a run starts, releases it when the run ends.

    One instance is shared across every concurrent run in the process. The lock
    serialises the check-and-reserve step so two dispatches racing the same ledger
    can never both pass.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        self._reservations: dict[str, Reservation] = {}

    @property
    def budget(self) -> BudgetConfig:
        return self._config.budget

    def _reserved_usd(self, company_id: int) -> float:
        """Dollars already claimed by other in-flight runs for this company."""
        return sum(r.usd for r in self._reservations.values() if r.company_id == company_id)

    async def reserve(
        self, session: Session, *, company_id: int, model: str, agent_name: AgentName
    ) -> Reservation:
        """Claim one run's worth of budget, or raise :class:`BudgetExceeded`.

        The claim is always this agent's ``max_usd_per_run`` in full (its own
        override if it has one, else the global default, resolved through
        ``AppConfig.max_usd_per_run_for`` the same way every other enforcement path
        does): not priced from the model or from an assumed token count, because
        there is no reliable upper bound on input tokens before a run starts (a
        tool-heavy agent can pull far more input than it ever emits as output), and
        the configured dollar ceiling is the one figure both this reservation and
        the SDK's own ``max_budget_usd`` agree on.
        """
        async with self._lock:
            existing = active_halt(session, company_id)
            if existing is not None:
                raise BudgetExceeded(existing)

            today = utcday()
            run_usd = self._config.max_usd_per_run_for(agent_name)

            day_usd = (
                _actual_usd(session, company_id, since_day=today)
                + self._reserved_usd(company_id)
                + run_usd
            )
            if day_usd > self.budget.max_usd_per_day:
                halt = write_halt(
                    session,
                    company_id=company_id,
                    scope=BudgetScope.DAY,
                    limit_name="max_usd_per_day",
                    limit_value=self.budget.max_usd_per_day,
                    observed_value=day_usd,
                    period_key=today,
                    reason=(
                        f"Reserving ${run_usd:.4f} for a {model} run would bring "
                        f"today's committed-plus-reserved spend to ${day_usd:.4f}, "
                        f"over the ${self.budget.max_usd_per_day:.2f} daily ceiling."
                    ),
                )
                raise BudgetExceeded(halt)

            company_usd = (
                _actual_usd(session, company_id) + self._reserved_usd(company_id) + run_usd
            )
            if company_usd > self.budget.max_usd_per_company:
                halt = write_halt(
                    session,
                    company_id=company_id,
                    scope=BudgetScope.COMPANY,
                    limit_name="max_usd_per_company",
                    limit_value=self.budget.max_usd_per_company,
                    observed_value=company_usd,
                    period_key=None,
                    reason=(
                        f"Reserving ${run_usd:.4f} for a {model} run would bring the "
                        f"company's lifetime spend to ${company_usd:.4f}, over the "
                        f"${self.budget.max_usd_per_company:.2f} lifetime ceiling."
                    ),
                )
                raise BudgetExceeded(halt)

            reservation = Reservation(
                id=str(uuid.uuid4()), company_id=company_id, model=model, usd=run_usd
            )
            self._reservations[reservation.id] = reservation
            return reservation

    def remaining_today_usd(self, session: Session, company_id: int) -> float:
        """Today's daily ceiling minus committed spend and in-flight reservations:
        the exact figure :meth:`reserve` checks against, not a separate estimate.

        The planner's one source of truth for "how much is left today" -- found
        the hard way: it was previously given no such figure at all, and once
        mis-quoted an unrelated run's cost ceiling as this number. Can be
        negative (an open halt does not zero this out, it just means the last
        reservation that crossed it never got to spend).
        """
        today = utcday()
        committed_or_reserved = _actual_usd(
            session, company_id, since_day=today
        ) + self._reserved_usd(company_id)
        return self.budget.max_usd_per_day - committed_or_reserved

    def release(self, reservation: Reservation) -> None:
        """Give back a reservation. Safe to call more than once; the second call
        finds nothing and does nothing, which matters in a ``finally`` block that
        might run after an earlier explicit release on the same path."""
        self._reservations.pop(reservation.id, None)

    def check_run_did_not_overshoot(
        self, session: Session, *, company_id: int, run: Run
    ) -> list[BudgetHalt]:
        """Catch a run whose *actual* usage exceeded its own reservation.

        Two independent checks, either or both of which can fire:

        - ``max_usd_per_run``: the reservation's own unit. If the SDK's own
          ``max_budget_usd`` let a run through over this, that enforcement has a
          gap worth knowing about, not papering over.
        - ``max_tokens_per_run``: the same-model safety net. Unrelated to cost, so a
          cheap model generating an absurd number of tokens trips this independently
          of whether it also cost more than expected.

        Both are detection after the fact, not prevention: the spend already
        happened. They exist so a mis-estimate is caught loudly instead of quietly
        compounding on the next run.
        """
        halts: list[BudgetHalt] = []
        max_usd = self._config.max_usd_per_run_for(run.agent)
        max_tokens = self._config.max_tokens_per_run_for(run.agent)

        if run.cost_usd > max_usd:
            halts.append(
                write_halt(
                    session,
                    company_id=company_id,
                    run_id=run.id,
                    scope=BudgetScope.RUN,
                    limit_name="max_usd_per_run",
                    limit_value=max_usd,
                    observed_value=run.cost_usd,
                    period_key=None,
                    reason=(
                        f"Run {run.id} ({run.agent.value}) cost ${run.cost_usd:.4f} against "
                        f"a reservation of ${max_usd:.2f}. max_budget_usd was passed to the "
                        "SDK for this exact figure; this means that enforcement did not "
                        "hold, not that the ceiling does not apply."
                    ),
                )
            )

        if run.total_tokens > max_tokens:
            halts.append(
                write_halt(
                    session,
                    company_id=company_id,
                    run_id=run.id,
                    scope=BudgetScope.RUN,
                    limit_name="max_tokens_per_run",
                    limit_value=max_tokens,
                    observed_value=run.total_tokens,
                    period_key=None,
                    reason=(
                        f"Run {run.id} ({run.agent.value}) used {run.total_tokens} tokens "
                        f"against a same-model safety net of {max_tokens}."
                    ),
                )
            )

        return halts


def reconcile_orphaned_runs(session: Session, app_config: AppConfig) -> list[Run]:
    """Find every run left in ``running`` from a previous process, and close it out.

    Call this exactly once, at process start, before the scheduler's first tick.
    Within a single live process a ``running`` row only exists for the duration of
    one in-flight SDK call; any such row still present when a *new* process starts
    can only be left over from one that died mid-run.

    That run was dispatched, which means it may have already been billed by
    Anthropic, and there is no way now to learn what it actually used: the process
    that would have read the result is the one that died. Recording it as zero
    spend would under-count real money, and under-counting is exactly what let a
    ceiling silently be crossed in the first place. So it is priced at its
    reservation's worst case, ``max_usd_per_run``, the same figure that was held
    against the ledger while it ran, and marked ``ORPHANED`` rather than ``FAILED``:
    a task that failed and one whose actual outcome is simply unknown are different
    facts, and collapsing them would hide which one happened.

    After every orphan is priced in, the day and company ceilings are re-checked
    against that now-worse ledger, per company. If the worst case alone is over a
    ceiling, a halt is written immediately: the point of pricing the worst case is
    exactly so this can catch it, rather than a healthy-looking ledger staying
    healthy-looking until the real bill arrives.
    """
    orphans = list(session.execute(select(Run).where(Run.status == RunStatus.RUNNING)).scalars())
    if not orphans:
        return []

    now = utcnow()
    fx_rate = app_config.budget.usd_to_gbp
    affected_companies: set[int] = set()

    for run in orphans:
        # Resolved per run, not once for the whole batch: two orphans can belong
        # to different agents, each with its own reservation figure.
        worst_case = app_config.max_usd_per_run_for(run.agent)
        run.status = RunStatus.ORPHANED
        run.cost_usd = worst_case
        run.cost_gbp = worst_case * fx_rate
        run.fx_rate = fx_rate
        run.finished_at = now
        run.error = (
            "The process was interrupted while this run was in progress. Real usage "
            "is unknown; cost is recorded at the reservation's worst case "
            f"(${worst_case:.2f}) for budget safety, not assumed to be zero."
        )
        affected_companies.add(run.company_id)
    session.commit()

    for company_id in affected_companies:
        _log_orphan_recovery(session, app_config, company_id)

    return orphans


def _log_orphan_recovery(session: Session, app_config: AppConfig, company_id: int) -> None:
    log(
        session,
        company_id=company_id,
        kind=ActivityKind.ERROR,
        summary="Recovered from an interrupted process: orphaned run(s) priced at worst case",
        error="See the affected Run row(s) for detail.",
    )

    today = utcday()
    day_usd = _actual_usd(session, company_id, since_day=today)
    if day_usd > app_config.budget.max_usd_per_day:
        write_halt(
            session,
            company_id=company_id,
            scope=BudgetScope.DAY,
            limit_name="max_usd_per_day",
            limit_value=app_config.budget.max_usd_per_day,
            observed_value=day_usd,
            period_key=today,
            reason=(
                "Worst-case pricing of a run orphaned by an interrupted process "
                f"brings today's spend to ${day_usd:.4f}, over the "
                f"${app_config.budget.max_usd_per_day:.2f} daily ceiling."
            ),
        )

    company_usd = _actual_usd(session, company_id)
    if company_usd > app_config.budget.max_usd_per_company:
        write_halt(
            session,
            company_id=company_id,
            scope=BudgetScope.COMPANY,
            limit_name="max_usd_per_company",
            limit_value=app_config.budget.max_usd_per_company,
            observed_value=company_usd,
            period_key=None,
            reason=(
                "Worst-case pricing of a run orphaned by an interrupted process "
                f"brings lifetime spend to ${company_usd:.4f}, over the "
                f"${app_config.budget.max_usd_per_company:.2f} lifetime ceiling."
            ),
        )


def write_off_orphan(
    session: Session,
    run: Run,
    *,
    actual_cost_usd: float,
    decided_by: str,
    note: str = "",
) -> Run:
    """Correct an ORPHANED run's worst-case pricing to a confirmed real figure.

    Nothing in this system can learn a crashed run's actual usage on its own: the
    process that would have read the result is the one that died. This is the
    deliberate human override for when that figure becomes known some other way (a
    check against the Anthropic console, or a confirmation that the call never
    actually reached the API and the real cost is zero). ``actual_cost_usd`` is
    required, never defaulted, so every write-off is a stated decision, not a
    guess.

    Moves the run to RECONCILED rather than back to a spent-looking status, so a
    query can always tell "still priced at worst case" apart from "a human has
    confirmed this figure". Writes an ORPHAN_WRITTEN_OFF activity event recording
    the old and new values, who decided it and why: that event is the audit trail,
    there is no separate table for it.

    Does **not** clear any BudgetHalt the original worst-case pricing may have
    tripped. That stays a separate, deliberate act, the same as every other halt:
    the ledger being corrected downward is not, by itself, permission to resume.
    """
    if run.status != RunStatus.ORPHANED:
        raise ValueError(
            f"Run {run.id} is {run.status}, not orphaned. write_off_orphan only "
            "corrects the worst-case price reconcile_orphaned_runs assigned; it is "
            "not a general way to edit a run's recorded cost."
        )
    if actual_cost_usd < 0:
        raise ValueError(
            f"A cost of ${actual_cost_usd:.4f} is negative, which would reduce the ledger "
            "below what was actually spent."
        )

    previous_cost_usd = run.cost_usd
    fx_rate = run.fx_rate or 1.0
    run.status = RunStatus.RECONCILED
    run.cost_usd = actual_cost_usd
    run.cost_gbp = actual_cost_usd * fx_rate

    log(
        session,
        company_id=run.company_id,
        kind=ActivityKind.ORPHAN_WRITTEN_OFF,
        summary=(
            f"Run {run.id} written off: ${previous_cost_usd:.4f} worst-case "
            f"corrected to ${actual_cost_usd:.4f}"
        ),
        detail={
            "previous_cost_usd": previous_cost_usd,
            "actual_cost_usd": actual_cost_usd,
            "decided_by": decided_by,
            "note": note,
        },
        run_id=run.id,
    )
    session.commit()
    return run
