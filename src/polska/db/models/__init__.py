"""Every mapped table.

Importing this package registers all models on ``Base.metadata``, which is what
Alembic autogenerate and ``create_all`` both rely on.
"""

from __future__ import annotations

from polska.db.models.activity import ActivityEvent
from polska.db.models.approval import Approval
from polska.db.models.budget import BudgetHalt
from polska.db.models.company import Company
from polska.db.models.goal import Goal
from polska.db.models.run import Run
from polska.db.models.task import Task

__all__ = [
    "ActivityEvent",
    "Approval",
    "BudgetHalt",
    "Company",
    "Goal",
    "Run",
    "Task",
]
