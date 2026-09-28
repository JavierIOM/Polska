"""Budget halts: the record of why the scheduler stopped."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import Enum, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from polska.db.base import Base
from polska.db.enums import BudgetScope
from polska.db.types import UTCDateTime, utcnow

if TYPE_CHECKING:
    from polska.db.models.company import Company
    from polska.db.models.run import Run


class BudgetHalt(Base):
    """A ceiling that was crossed, and the stop it caused.

    Running totals are not cached here. They are summed from ``runs``, which is the
    only place tokens are recorded, so the two can never disagree. This table holds
    only the exceptional event.

    An open halt, meaning ``cleared_at`` is null, blocks the scheduler. Clearing one is
    a deliberate human act from the dashboard, never automatic: the budget guard must
    not be able to talk itself back into spending.
    """

    __tablename__ = "budget_halts"
    __table_args__ = (Index("ix_budget_halts_company_cleared", "company_id", "cleared_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Null means a global halt affecting every company.
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=True, index=True
    )

    scope: Mapped[BudgetScope] = mapped_column(
        Enum(BudgetScope, native_enum=False, length=32, validate_strings=True)
    )
    #: The specific run that tripped this, when there is one. Null for a
    #: DAY/COMPANY-scoped halt, which is never about one single run. Set to
    #: null rather than deleted if that run is ever removed, the same as
    #: Approval.run_id: the halt's own record of what happened must survive
    #: the run row it points at.
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Which ceiling, e.g. "max_usd_per_day".
    limit_name: Mapped[str] = mapped_column(String(80))
    limit_value: Mapped[float] = mapped_column(Float)
    observed_value: Mapped[float] = mapped_column(Float)
    #: For a daily scope, the UTC day the totals were taken over.
    period_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    reason: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    cleared_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cleared_by: Mapped[str | None] = mapped_column(String(80), nullable=True)

    company: Mapped[Company | None] = relationship(back_populates="halts")
    run: Mapped[Run | None] = relationship(back_populates="halts")

    @property
    def is_active(self) -> bool:
        """True if this halt is still stopping work."""
        return self.cleared_at is None

    @property
    def overshoot(self) -> float:
        """How far past the ceiling the run got before it was caught."""
        return self.observed_value - self.limit_value

    def __repr__(self) -> str:
        state = "active" if self.is_active else "cleared"
        return f"<BudgetHalt {self.id} {self.scope}/{self.limit_name} {state}>"
