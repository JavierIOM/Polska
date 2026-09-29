"""Every enumerated value that reaches the database.

Stored as VARCHAR with a CHECK constraint rather than a native enum, because SQLite
has no enum type and a CHECK keeps the values readable in a sqlite3 shell.
"""

from __future__ import annotations

from enum import StrEnum


class GoalStatus(StrEnum):
    ACTIVE = "active"
    ACHIEVED = "achieved"
    PAUSED = "paused"
    ABANDONED = "abandoned"


class TaskType(StrEnum):
    ENGINEERING = "engineering"
    MARKETING = "marketing"
    SUPPORT = "support"
    RESEARCH = "research"


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    FAILED = "failed"
    ABANDONED = "abandoned"
    #: The agent determined the environment cannot execute this task at all (no
    #: runtime to run a test, no way to verify a change), and said so, rather than
    #: retrying identically or improvising a workaround. Deliberately distinct from
    #: FAILED/ABANDONED: those mean the work was attempted and didn't succeed;
    #: BLOCKED means the work was never executable here, which is a fact about the
    #: environment, not the task or the attempt. See AgentResult.blocked_reason and
    #: RunStatus.ENVIRONMENT_BLOCKED.
    BLOCKED = "blocked"


class AgentName(StrEnum):
    """Who ran. The planner and the dedup judge are agents too, and their cost counts."""

    PLANNER = "planner"
    DEDUP_JUDGE = "dedup_judge"
    ENGINEER = "engineer"
    MARKETER = "marketer"
    SUPPORT = "support"
    ANALYST = "analyst"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BUDGET_BLOCKED = "budget_blocked"
    #: Polska itself cut this run off mid-stream because it crossed its own token
    #: or dollar ceiling before finishing. Real money was spent, unlike
    #: BUDGET_BLOCKED (which never calls the SDK at all): distinguished from it
    #: precisely so "we refused to start" and "we started and then stopped it
    #: ourselves" are never conflated in the record. See AgentRunner._execute_and_record.
    INTERRUPTED = "interrupted"
    #: The process that started this run died before it could record what happened.
    #: Real money may have been spent; actual usage is unknown. Never confused with
    #: FAILED, which means the run finished and we know why. Recovered at the next
    #: process start by reconcile_orphaned_runs, which prices it at the worst case
    #: (its reservation) rather than assuming zero.
    ORPHANED = "orphaned"
    #: An ORPHANED run whose cost has since been confirmed by a human and corrected
    #: from the worst-case estimate to a real figure (or to zero, if it never
    #: actually billed). Never reached automatically: see write_off_orphan.
    RECONCILED = "reconciled"
    INVALID_OUTPUT = "invalid_output"


class Reversibility(StrEnum):
    """Recorded on every proposed action, not only the ones that need approval.

    Reversible actions execute directly. Irreversible ones always stop at the gate.
    """

    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    EXECUTION_FAILED = "execution_failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class BudgetScope(StrEnum):
    RUN = "run"
    DAY = "day"
    COMPANY = "company"


class ActivityKind(StrEnum):
    """Feed entries. Deliberately coarse: the feed is for reading, not querying."""

    COMPANY_LOADED = "company_loaded"
    GOAL_CREATED = "goal_created"
    GOAL_UPDATED = "goal_updated"
    PLAN_STARTED = "plan_started"
    PLAN_COMPLETED = "plan_completed"
    TASK_ENQUEUED = "task_enqueued"
    TASK_DEDUPED = "task_deduped"
    TASK_STATE_CHANGED = "task_state_changed"
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    ACTION_PROPOSED = "action_proposed"
    ACTION_EXECUTED = "action_executed"
    APPROVAL_DECIDED = "approval_decided"
    BUDGET_HALT = "budget_halt"
    BUDGET_RESUMED = "budget_resumed"
    ORPHAN_WRITTEN_OFF = "orphan_written_off"
    SCHEDULER_TICK = "scheduler_tick"
    ERROR = "error"
