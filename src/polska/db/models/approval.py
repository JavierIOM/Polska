"""A proposed external side effect, parked until a human says yes or no."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Enum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from polska.db.base import Base
from polska.db.enums import ApprovalStatus, Reversibility
from polska.db.types import UTCDateTime, utcnow

if TYPE_CHECKING:
    from polska.db.models.company import Company
    from polska.db.models.run import Run
    from polska.db.models.task import Task

#: Statuses from which no further decision is possible.
_CLOSED = frozenset(
    {
        ApprovalStatus.REJECTED,
        ApprovalStatus.EXECUTED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.CANCELLED,
    }
)


class Approval(Base):
    """An action the agent wants to take, stored in full rather than performed.

    ``payload`` is the complete, replayable adapter call. Approving it executes that
    stored payload verbatim: the agent is never asked again, and never gets a second
    chance to decide what the action was. That is the whole point of the gate, so the
    payload must be enough on its own to perform the action.

    ``preview`` is the human-readable rendering of the same thing. If the two ever
    disagree, the preview is the bug, because the payload is what runs.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_company_status", "company_id", "status"),
        Index("ix_approvals_status_requested", "status", "requested_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: Dotted action identifier, e.g. "email.send" or "social.publish".
    action_type: Mapped[str] = mapped_column(String(80), index=True)
    #: Which integration adapter will carry it out, e.g. "dry_run".
    adapter: Mapped[str] = mapped_column(String(60))
    #: Recorded even though only irreversible actions reach this table, so an audit can
    #: show the classifier's verdict rather than inferring it from the row's existence.
    reversibility: Mapped[Reversibility] = mapped_column(
        Enum(Reversibility, native_enum=False, length=32, validate_strings=True),
        default=Reversibility.IRREVERSIBLE,
    )

    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    preview: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, native_enum=False, length=32, validate_strings=True),
        default=ApprovalStatus.PENDING,
        index=True,
    )
    decided_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: True when config auto-approved this action type rather than a human deciding.
    auto_approved: Mapped[bool] = mapped_column(default=False)

    #: What the adapter returned. Written whether execution succeeded or failed.
    execution_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    execution_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    requested_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    executed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: Stale approvals should not fire days later against changed circumstances.
    expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    company: Mapped[Company] = relationship(back_populates="approvals")
    task: Mapped[Task | None] = relationship(back_populates="approvals")
    run: Mapped[Run | None] = relationship(back_populates="approvals")

    @property
    def is_pending(self) -> bool:
        """True if this is still sitting in the queue waiting on a decision."""
        return self.status == ApprovalStatus.PENDING

    @property
    def is_closed(self) -> bool:
        """True if no further decision is possible."""
        return self.status in _CLOSED

    def is_expired(self, now: dt.datetime | None = None) -> bool:
        """True if the window to act on this has passed."""
        if self.expires_at is None:
            return False
        return (now or utcnow()) >= self.expires_at

    def __repr__(self) -> str:
        return f"<Approval {self.id} {self.action_type} {self.status}>"
