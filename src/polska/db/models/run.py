"""One agent invocation against one task."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Enum, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from polska.db.base import Base
from polska.db.enums import AgentName, RunStatus
from polska.db.types import UTCDateTime, utcnow

if TYPE_CHECKING:
    from polska.db.models.approval import Approval
    from polska.db.models.company import Company
    from polska.db.models.task import Task


class Run(Base):
    """A single call to an agent, with everything needed to explain the bill.

    Cost is stored in both currencies at the moment of the run. USD is what Anthropic
    charges, GBP is what Javier reads, and freezing the converted figure means a later
    change to the configured FX rate cannot rewrite history.

    ``task_id`` is nullable: the planner and the dedup judge run against a company
    rather than a task, and their tokens count the same as anyone else's.
    """

    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_company_started", "company_id", "started_at"),
        Index("ix_runs_company_status", "company_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )

    agent: Mapped[AgentName] = mapped_column(
        Enum(AgentName, native_enum=False, length=32, validate_strings=True), index=True
    )
    model: Mapped[str] = mapped_column(String(80))
    #: Claude Agent SDK session, for correlating with SDK-side logs.
    session_id: Mapped[str | None] = mapped_column(String(80), nullable=True)

    system_prompt: Mapped[str] = mapped_column(Text, default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    #: Whatever the agent actually emitted, before any parsing or validation. Kept
    #: verbatim so a schema rejection can be diagnosed after the fact.
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Ordered list of tool calls: name, input, and whether it errored.
    tools_called: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)

    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cost_gbp: Mapped[float] = mapped_column(Float, default=0.0)
    #: The USD to GBP rate used, so the conversion can be checked later.
    fx_rate: Mapped[float] = mapped_column(Float, default=1.0)

    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, native_enum=False, length=32, validate_strings=True),
        default=RunStatus.RUNNING,
        index=True,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    company: Mapped[Company] = relationship(back_populates="runs")
    task: Mapped[Task | None] = relationship(back_populates="runs")
    approvals: Mapped[list[Approval]] = relationship(back_populates="run")

    @property
    def total_tokens(self) -> int:
        """Billable tokens. Cache reads are charged, so they count.

        Cache creation is counted too: it is billed at a premium, and leaving it out
        would let a prompt-caching agent quietly overshoot its per-run ceiling.
        """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    def __repr__(self) -> str:
        return (
            f"<Run {self.id} {self.agent}/{self.model} {self.status} "
            f"{self.total_tokens}tok ${self.cost_usd:.4f}>"
        )
