"""The company a run of Polska operates."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from polska.db.base import Base
from polska.db.types import UTCDateTime, utcnow

if TYPE_CHECKING:
    from polska.db.models.approval import Approval
    from polska.db.models.budget import BudgetHalt
    from polska.db.models.goal import Goal
    from polska.db.models.run import Run
    from polska.db.models.task import Task


class Company(Base):
    """One business, loaded from a YAML profile on disk.

    The YAML is the source of truth for the idea, voice and constraints. This row is a
    snapshot of the last load: ``profile_hash`` tells the orchestrator whether the file
    on disk has moved on since, so a changed brief can be picked up without a restart.
    """

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))

    #: Path the profile was loaded from, relative to the repo root.
    profile_path: Mapped[str] = mapped_column(String(500))
    #: SHA-256 of the raw YAML, to detect edits on disk.
    profile_hash: Mapped[str] = mapped_column(String(64), index=True)
    #: The validated CompanyProfile, dumped. Full fidelity, queried rarely.
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    #: Broken out of the profile because the planner prompt reads them every tick.
    idea: Mapped[str] = mapped_column(Text, default="")
    brand_voice: Mapped[str] = mapped_column(Text, default="")

    #: Paused companies stay in the database but are skipped by the scheduler.
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    goals: Mapped[list[Goal]] = relationship(back_populates="company", cascade="all, delete-orphan")
    tasks: Mapped[list[Task]] = relationship(back_populates="company", cascade="all, delete-orphan")
    runs: Mapped[list[Run]] = relationship(back_populates="company", cascade="all, delete-orphan")
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    halts: Mapped[list[BudgetHalt]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Company {self.slug!r} active={self.active}>"
