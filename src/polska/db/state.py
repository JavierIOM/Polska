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
#: running           -> finished, failed, stopped at the approval gate, or found
#:                      to be unexecutable in this environment
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
            TaskState.BLOCKED,
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
    TaskState.BLOCKED: frozenset(),
}

#: States a task can never leave. Reaching one of these is the end of its life.
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {state for state, allowed in TRANSITIONS.items() if not allowed}
)

#: States that count against the max-concurrent ceiling.
ACTIVE_STATES: frozenset[TaskState] = frozenset({TaskState.RUNNING, TaskState.AWAITING_APPROVAL})

#: Work still in flight. Not, on its own, the right set to dedup against: see
#: DEDUP_SUPPRESSING_STATES below. FAILED is deliberately not open (it does not count
#: against the concurrency ceiling) but IS a dedup suppressor, which is why the two
#: concepts have separate constants rather than sharing this one.
OPEN_STATES: frozenset[TaskState] = frozenset(
    {TaskState.QUEUED, TaskState.RUNNING, TaskState.AWAITING_APPROVAL}
)

#: States that must suppress a fresh planner proposal for the same work,
#: unconditionally, with no lookback window.
#:
#: FAILED belongs here even though it is not "open": a failed task with attempts
#: remaining is the orchestrator's own retry queue, and a fresh proposal for the same
#: work would race that automatic retry rather than replace it.
#:
#: DONE is handled separately by the caller (see DEDUP_LOOKBACK_STATES) because it
#: should only suppress inside the configured lookback window, not forever.
DEDUP_SUPPRESSING_STATES: frozenset[TaskState] = OPEN_STATES | {TaskState.FAILED}

#: DONE suppresses a duplicate proposal, but only inside dedup.lookback_days. Kept as
#: its own set of one so the caller's query makes the time-boxing explicit rather than
#: burying it in a magic exception.
DEDUP_LOOKBACK_STATES: frozenset[TaskState] = frozenset({TaskState.DONE})

#: ABANDONED and BLOCKED must never suppress a proposal, at any age. ABANDONED means
#: the system tried and gave up; BLOCKED means the environment couldn't run it at
#: all. Neither is a "this need doesn't exist" signal, and it is a bug, not a
#: feature, for either to quietly stop the same genuine need from ever being
#: proposed again -- a BLOCKED task in particular may become executable the moment
#: the environment gap it hit is fixed. Named here, rather than left as "whatever
#: is not in the two sets above", so the exclusion is a decision on the page, not
#: an accident of set arithmetic.
DEDUP_NEVER_SUPPRESSES: frozenset[TaskState] = frozenset({TaskState.ABANDONED, TaskState.BLOCKED})


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
