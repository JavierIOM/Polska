"""A unit of work the orchestrator proposed and a worker agent executes."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from polska.db.base import Base
from polska.db.enums import TaskState, TaskType
from polska.db.state import assert_transition, is_terminal
from polska.db.types import UTCDateTime, utcnow

if TYPE_CHECKING:
    from polska.db.models.approval import Approval
    from polska.db.models.company import Company
    from polska.db.models.goal import Goal
    from polska.db.models.run import Run


class Task(Base):
    """One piece of work, its state machine, and whatever it produced.

    State is only ever changed through :meth:`transition_to`. Assigning to ``state``
    directly bypasses the machine and is a bug, not a shortcut.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_company_state", "company_id", "state"),
        Index("ix_tasks_company_dedup", "company_id", "dedup_key"),
        Index("ix_tasks_state_created", "state", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    #: Nullable because housekeeping tasks serve no goal. The planner schema still
    #: requires one, so anything arriving from an agent will always have it set.
    goal_id: Mapped[int | None] = mapped_column(
        ForeignKey("goals.id", ondelete="SET NULL"), nullable=True, index=True
    )

    type: Mapped[TaskType] = mapped_column(
        Enum(TaskType, native_enum=False, length=32, validate_strings=True), index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")

    state: Mapped[TaskState] = mapped_column(
        Enum(TaskState, native_enum=False, length=32, validate_strings=True),
        default=TaskState.QUEUED,
        index=True,
    )
    #: Lower sorts first, copied from the planner's proposal.
    priority: Mapped[int] = mapped_column(Integer, default=100)

    #: Why the planner thought this was worth doing. Shown on the dashboard.
    rationale: Mapped[str] = mapped_column(Text, default="")

    #: Normalised fingerprint used to spot repeated work. See the dedup module.
    dedup_key: Mapped[str] = mapped_column(String(200), default="", index=True)
    #: How the dedup decision was reached, including the score and which task it
    #: matched. Written for every task, kept or dropped, so the choice is auditable.
    dedup_note: Mapped[str] = mapped_column(Text, default="")

    #: The full result payload from the worker agent. Schema depends on task type.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    #: Populated when the task lands in ``failed``.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Incremented each time the task enters ``running``. Caps retries.
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    company: Mapped[Company] = relationship(back_populates="tasks")
    goal: Mapped[Goal | None] = relationship(back_populates="tasks")
    runs: Mapped[list[Run]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="Run.id"
    )
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="Approval.id"
    )

    def transition_to(
        self,
        target: TaskState,
        *,
        error: str | None = None,
        result: dict[str, Any] | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        """Move the task to ``target``, or raise ``IllegalTransition``.

        Also keeps the timestamps and the attempt counter honest, so no caller has to
        remember to set ``started_at`` alongside the state.
        """
        assert_transition(self.state, target)
        moment = now or utcnow()

        if target == TaskState.RUNNING:
            self.attempts += 1
            if self.started_at is None:
                self.started_at = moment
            # A retry or a resume clears the previous failure.
            self.error = None
            self.finished_at = None

        if target == TaskState.FAILED:
            self.error = error
        elif error is not None:
            raise ValueError(
                f"An error message was supplied for a transition to {target}, "
                "but only a move to 'failed' records one."
            )

        if result is not None:
            self.result = result

        if is_terminal(target):
            self.finished_at = moment

        self.state = target
        self.updated_at = moment

    @property
    def is_terminal(self) -> bool:
        """True if the task will never move again."""
        return is_terminal(self.state)

    @property
    def duration_seconds(self) -> float | None:
        """Wall-clock seconds from first start to finish, if it has finished."""
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def __repr__(self) -> str:
        return f"<Task {self.id} {self.type}/{self.state} {self.title!r}>"
