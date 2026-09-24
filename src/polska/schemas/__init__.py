"""Pydantic contracts for everything an agent returns.

Nothing in this project reads an agent response by string-matching. An output either
validates against one of these models or the run is recorded as ``invalid_output``.
"""

from __future__ import annotations

from polska.schemas.actions import ActionRequest, AgentResult
from polska.schemas.planner import (
    DedupJudgeOutput,
    DedupVerdict,
    PlannerOutput,
    ProposedTask,
)

__all__ = [
    "ActionRequest",
    "AgentResult",
    "DedupJudgeOutput",
    "DedupVerdict",
    "PlannerOutput",
    "ProposedTask",
]
