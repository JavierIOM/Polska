"""A measurable objective the orchestrator plans towards."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import Enum, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from polska.db.base import Base
from polska.db.enums import GoalStatus
from polska.db.types import UTCDateTime, utcnow

if TYPE_CHECKING:
    from polska.db.models.company import Company
    from polska.db.models.task import Task


class Goal(Base):
    """Something the company is trying to achieve, with a number attached.

    The target is deliberately a single float plus a unit rather than a free-text
    aspiration. A goal the planner cannot measure is a goal it will argue with itself
    about forever.
    """

    __tablename__ = "goals"
    __table_args__ = (
        Index("ix_goals_company_status", "company_id", "status"),
        Index("ix_goals_company_key", "company_id", "key", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )

    #: The stable identifier from the company profile's YAML (GoalSpec.key), unique
    #: per company. This is what a planner proposal's ``goal_key`` resolves against;
    #: nothing else in the database is stable enough to key a goal by, since the
    #: title is free text the profile can edit at any time.
    key: Mapped[str] = mapped_column(String(64))

    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")

    #: What is being counted, e.g. "newsletter_subscribers".
    metric: Mapped[str] = mapped_column(String(120))
    target_value: Mapped[float] = mapped_column(Float)
    current_value: Mapped[float] = mapped_column(Float, default=0.0)
    unit: Mapped[str] = mapped_column(String(40), default="count")

    status: Mapped[GoalStatus] = mapped_column(
        Enum(GoalStatus, native_enum=False, length=32, validate_strings=True),
        default=GoalStatus.ACTIVE,
        index=True,
    )
    #: Lower sorts first. The planner is told the ordering.
    priority: Mapped[int] = mapped_column(Integer, default=100)

    due_date: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    achieved_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    company: Mapped[Company] = relationship(back_populates="goals")
    tasks: Mapped[list[Task]] = relationship(back_populates="goal")

    @property
    def progress(self) -> float:
        """Fraction of the target reached, clamped to 0.0 to 1.0.

        A zero target would be a division by zero, and also a meaningless goal, so it
        reports as complete rather than blowing up the dashboard.
        """
        if self.target_value == 0:
            return 1.0
        return max(0.0, min(1.0, self.current_value / self.target_value))

    @property
    def is_open(self) -> bool:
        """True if the planner should still be proposing work against this goal."""
        return self.status == GoalStatus.ACTIVE

    def __repr__(self) -> str:
        return (
            f"<Goal {self.id} {self.title!r} "
            f"{self.current_value}/{self.target_value} {self.unit} {self.status}>"
        )
