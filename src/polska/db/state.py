"""The task state machine.

One place defines what may follow what. Nothing else in the codebase is allowed to
assign to ``Task.state`` directly: everything goes through :func:`assert_transition`
so an illegal move raises instead of quietly corrupting the queue.
"""

from __future__ import annotations

from polska.db.enums import TaskState

#: Allowed transitions, keyed by the state being left.
#:
#: queued            -> picked up by a worker, or dropped before it ever ran
#: running           -> finished, failed, or stopped at the approval gate
#: awaiting_approval -> resumes (approved), abandoned (rejected), or the run died waiting
#: failed            -> requeued for a bounded retry, or given up on
TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset({TaskState.RUNNING, TaskState.ABANDONED, TaskState.FAILED}),
    TaskState.RUNNING: frozenset(
        {
            TaskState.AWAITING_APPROVAL,
            TaskState.DONE,
            TaskState.FAILED,
            TaskState.ABANDONED,
        }
    ),
    TaskState.AWAITING_APPROVAL: frozenset(
        {
            TaskState.RUNNING,
            TaskState.DONE,
            TaskState.FAILED,
            TaskState.ABANDONED,
        }
    ),
    TaskState.FAILED: frozenset({TaskState.QUEUED, TaskState.ABANDONED}),
    TaskState.DONE: frozenset(),
    TaskState.ABANDONED: frozenset(),
}

#: States a task can never leave. Reaching one of these is the end of its life.
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {state for state, allowed in TRANSITIONS.items() if not allowed}
)

#: States that count against the max-concurrent ceiling.
ACTIVE_STATES: frozenset[TaskState] = frozenset({TaskState.RUNNING, TaskState.AWAITING_APPROVAL})

#: States the planner must consider when deduplicating new proposals.
OPEN_STATES: frozenset[TaskState] = frozenset(
    {TaskState.QUEUED, TaskState.RUNNING, TaskState.AWAITING_APPROVAL}
)


class IllegalTransition(Exception):
    """Raised when something tries to move a task somewhere it cannot go."""

    def __init__(self, current: TaskState, target: TaskState) -> None:
        self.current = current
        self.target = target
        allowed = sorted(TRANSITIONS.get(current, frozenset()))
        allowed_text = ", ".join(allowed) if allowed else "nothing, it is terminal"
        super().__init__(
            f"Cannot move a task from {current} to {target}. "
            f"From {current} the only legal targets are: {allowed_text}."
        )


def can_transition(current: TaskState, target: TaskState) -> bool:
    """True if ``current`` may legally become ``target``."""
    return target in TRANSITIONS.get(current, frozenset())


def assert_transition(current: TaskState, target: TaskState) -> None:
    """Raise :class:`IllegalTransition` unless the move is legal.

    Self-transitions are illegal on purpose. A task moving from ``running`` to
    ``running`` is almost always a double-dispatch bug, and silence would hide it.
    """
    if not can_transition(current, target):
        raise IllegalTransition(current, target)


def is_terminal(state: TaskState) -> bool:
    """True if the task is finished and will never move again."""
    return state in TERMINAL_STATES
