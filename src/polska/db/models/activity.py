"""The chronological activity feed."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Enum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from polska.db.base import Base
from polska.db.enums import ActivityKind
from polska.db.types import UTCDateTime, utcnow

if TYPE_CHECKING:
    from polska.db.models.company import Company


class ActivityEvent(Base):
    """One line in the feed.

    Denormalised on purpose. The dashboard feed is a single indexed read rather than a
    five-way join, and an event keeps the summary it was written with even if the task
    it describes is later edited.

    This is also where the "log before you act" rule lands: an external effect writes
    ``ACTION_PROPOSED`` before the adapter is called, and ``ACTION_EXECUTED`` after, so
    a crash mid-call still leaves evidence that something was attempted.
    """

    __tablename__ = "activity_events"
    __table_args__ = (
        Index("ix_activity_company_created", "company_id", "created_at"),
        Index("ix_activity_kind_created", "kind", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=True, index=True
    )

    kind: Mapped[ActivityKind] = mapped_column(
        Enum(ActivityKind, native_enum=False, length=40, validate_strings=True), index=True
    )
    #: One line, already written for a human. No formatting done at render time.
    summary: Mapped[str] = mapped_column(String(500))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    #: Soft references. No foreign keys, so purging a task never erases its history.
    task_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    run_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    approval_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    #: Set when the event records a failure, so the feed can flag it.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    company: Mapped[Company | None] = relationship()

    def __repr__(self) -> str:
        return f"<ActivityEvent {self.id} {self.kind} {self.summary[:40]!r}>"
