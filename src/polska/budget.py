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
``asyncio.Lock`` is enough. A crash loses outstanding reservations, never actual spend
(nothing was committed to ``runs`` for a run that never finished), so the guard can
only ever under-count on restart, never over-count, and it can never wrongly open a
gate that should stay shut.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from polska.config.appconfig import AppConfig, BudgetConfig
from polska.db.enums import BudgetScope
from polska.db.models import BudgetHalt, Run
from polska.db.types import utcday, utcnow


@dataclass(frozen=True, slots=True)
class Reservation:
    """A held claim against the budget for one in-flight run.

    ``tokens`` is always the run's full ceiling, not an estimate of what it will
    likely use. See the module docstring for why: it is what makes the guard exact
    rather than merely usually-right.
    """

    id: str
    company_id: int
    model: str
    tokens: int
    usd: float


class BudgetExceeded(Exception):
    """Raised when a reservation would cross a ceiling, or one already has.

    Carries the :class:`BudgetHalt` that was written (or that was already open), so
    the caller can report exactly why without re-deriving it.
    """

    def __init__(self, halt: BudgetHalt) -> None:
        self.halt = halt
        super().__init__(
            f"{halt.scope.value} ceiling '{halt.limit_name}' would be crossed: "
            f"{halt.observed_value:.0f} against a limit of {halt.limit_value:.0f}. "
            f"{halt.reason}"
        )


def _worst_case_usd(config: AppConfig, model: str, tokens: int) -> float:
    """The most a reservation of ``tokens`` could possibly cost.

    Priced entirely at the output rate, since output is always the more expensive
    side and a reservation exists to be a safe upper bound, not an estimate.
    """
    price = config.price_for(model)
    return price.cost_usd(input_tokens=0, output_tokens=tokens)


def _actual_tokens(session: Session, company_id: int, *, since_day: str | None = None) -> int:
    """Real token spend from committed ``Run`` rows. The only source of truth."""
    total_expr = func.coalesce(
        func.sum(
            Run.input_tokens + Run.output_tokens + Run.cache_read_tokens + Run.cache_creation_tokens
        ),
        0,
    )
    stmt = select(total_expr).where(Run.company_id == company_id)
    if since_day is not None:
        stmt = stmt.where(func.strftime("%Y-%m-%d", Run.started_at) == since_day)
    return int(session.execute(stmt).scalar_one())


def _actual_usd(session: Session, company_id: int, *, since_day: str | None = None) -> float:
    """Real dollar spend from committed ``Run`` rows."""
    total_expr = func.coalesce(func.sum(Run.cost_usd), 0.0)
    stmt = select(total_expr).where(Run.company_id == company_id)
    if since_day is not None:
        stmt = stmt.where(func.strftime("%Y-%m-%d", Run.started_at) == since_day)
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

    def _reserved_tokens(self, company_id: int) -> int:
        """Tokens already claimed by other in-flight runs for this company."""
        return sum(r.tokens for r in self._reservations.values() if r.company_id == company_id)

    def _reserved_usd(self, company_id: int) -> float:
        return sum(r.usd for r in self._reservations.values() if r.company_id == company_id)

    async def reserve(self, session: Session, *, company_id: int, model: str) -> Reservation:
        """Claim one run's worth of budget, or raise :class:`BudgetExceeded`.

        On success, the reservation is registered before the lock is released, so the
        very next call sees it. On failure, a :class:`BudgetHalt` row is committed
        before the exception is raised: the stop is recorded before it takes effect,
        matching the same "log before you act" rule the approval gate follows.
        """
        async with self._lock:
            existing = active_halt(session, company_id)
            if existing is not None:
                raise BudgetExceeded(existing)

            today = utcday()
            run_tokens = self.budget.max_tokens_per_run
            run_usd = _worst_case_usd(self._config, model, run_tokens)

            day_tokens = (
                _actual_tokens(session, company_id, since_day=today)
                + self._reserved_tokens(company_id)
                + run_tokens
            )
            if day_tokens > self.budget.max_tokens_per_day:
                halt = self._write_halt(
                    session,
                    company_id=company_id,
                    scope=BudgetScope.DAY,
                    limit_name="max_tokens_per_day",
                    limit_value=self.budget.max_tokens_per_day,
                    observed_value=day_tokens,
                    period_key=today,
                    reason=(
                        f"Reserving {run_tokens} tokens for a {model} run would bring "
                        f"today's committed-plus-reserved total to {day_tokens}, over "
                        f"the {self.budget.max_tokens_per_day} daily ceiling."
                    ),
                )
                raise BudgetExceeded(halt)

            company_tokens = (
                _actual_tokens(session, company_id) + self._reserved_tokens(company_id) + run_tokens
            )
            if company_tokens > self.budget.max_tokens_per_company:
                halt = self._write_halt(
                    session,
                    company_id=company_id,
                    scope=BudgetScope.COMPANY,
                    limit_name="max_tokens_per_company",
                    limit_value=self.budget.max_tokens_per_company,
                    observed_value=company_tokens,
                    period_key=None,
                    reason=(
                        f"Reserving {run_tokens} tokens for a {model} run would bring "
                        f"the company's lifetime total to {company_tokens}, over the "
                        f"{self.budget.max_tokens_per_company} lifetime ceiling."
                    ),
                )
                raise BudgetExceeded(halt)

            day_usd = (
                _actual_usd(session, company_id, since_day=today)
                + self._reserved_usd(company_id)
                + run_usd
            )
            if day_usd > self.budget.max_usd_per_day:
                halt = self._write_halt(
                    session,
                    company_id=company_id,
                    scope=BudgetScope.DAY,
                    limit_name="max_usd_per_day",
                    limit_value=self.budget.max_usd_per_day,
                    observed_value=day_usd,
                    period_key=today,
                    reason=(
                        f"Reserving worst-case ${run_usd:.4f} for a {model} run would "
                        f"bring today's committed-plus-reserved spend to ${day_usd:.4f}, "
                        f"over the ${self.budget.max_usd_per_day:.2f} daily ceiling."
                    ),
                )
                raise BudgetExceeded(halt)

            company_usd = (
                _actual_usd(session, company_id) + self._reserved_usd(company_id) + run_usd
            )
            if company_usd > self.budget.max_usd_per_company:
                halt = self._write_halt(
                    session,
                    company_id=company_id,
                    scope=BudgetScope.COMPANY,
                    limit_name="max_usd_per_company",
                    limit_value=self.budget.max_usd_per_company,
                    observed_value=company_usd,
                    period_key=None,
                    reason=(
                        f"Reserving worst-case ${run_usd:.4f} for a {model} run would "
                        f"bring the company's lifetime spend to ${company_usd:.4f}, over "
                        f"the ${self.budget.max_usd_per_company:.2f} lifetime ceiling."
                    ),
                )
                raise BudgetExceeded(halt)

            reservation = Reservation(
                id=str(uuid.uuid4()),
                company_id=company_id,
                model=model,
                tokens=run_tokens,
                usd=run_usd,
            )
            self._reservations[reservation.id] = reservation
            return reservation

    def release(self, reservation: Reservation) -> None:
        """Give back a reservation. Safe to call more than once; the second call
        finds nothing and does nothing, which matters in a ``finally`` block that
        might run after an earlier explicit release on the same path."""
        self._reservations.pop(reservation.id, None)

    def check_run_did_not_overshoot(
        self, session: Session, *, company_id: int, run: Run
    ) -> BudgetHalt | None:
        """Catch a run whose *actual* usage exceeded its own reservation.

        The reservation is sized at ``max_tokens_per_run`` precisely so this should
        never fire. If it does, something under-priced the worst case (a pricing
        table out of date, or a token count read wrong), and that is worth a halt and
        an investigation, not a shrug. This is detection after the fact, not
        prevention: the tokens are already spent. It exists so a mis-estimate is
        caught loudly instead of quietly compounding on the next run.
        """
        if run.total_tokens <= self.budget.max_tokens_per_run:
            return None
        return self._write_halt(
            session,
            company_id=company_id,
            scope=BudgetScope.RUN,
            limit_name="max_tokens_per_run",
            limit_value=self.budget.max_tokens_per_run,
            observed_value=run.total_tokens,
            period_key=None,
            reason=(
                f"Run {run.id} used {run.total_tokens} tokens against a reservation "
                f"of {self.budget.max_tokens_per_run}. The reservation is meant to be "
                "a hard upper bound; this means it was mis-sized, not that the ceiling "
                "does not apply."
            ),
        )

    def _write_halt(
        self,
        session: Session,
        *,
        company_id: int | None,
        scope: BudgetScope,
        limit_name: str,
        limit_value: float,
        observed_value: float,
        period_key: str | None,
        reason: str,
    ) -> BudgetHalt:
        halt = BudgetHalt(
            company_id=company_id,
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
