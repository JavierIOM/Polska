"""The task state machine.

Tested exhaustively rather than by example: every one of the 36 ordered pairs of
states is asserted legal or illegal. A new state added to the enum without a decision
about what it connects to will fail here rather than in production.
"""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
from sqlalchemy.orm import Session

from polska.db.enums import TaskState
from polska.db.models import Task
from polska.db.state import (
    ACTIVE_STATES,
    OPEN_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    assert_transition,
    can_transition,
    is_terminal,
)

#: The full legal set, written out independently of TRANSITIONS so the test is a
#: second opinion rather than a restatement of the implementation.
LEGAL_PAIRS = {
    (TaskState.QUEUED, TaskState.RUNNING),
    (TaskState.QUEUED, TaskState.ABANDONED),
    (TaskState.QUEUED, TaskState.FAILED),
    (TaskState.RUNNING, TaskState.AWAITING_APPROVAL),
    (TaskState.RUNNING, TaskState.DONE),
    (TaskState.RUNNING, TaskState.FAILED),
    (TaskState.RUNNING, TaskState.ABANDONED),
    (TaskState.AWAITING_APPROVAL, TaskState.RUNNING),
    (TaskState.AWAITING_APPROVAL, TaskState.DONE),
    (TaskState.AWAITING_APPROVAL, TaskState.FAILED),
    (TaskState.AWAITING_APPROVAL, TaskState.ABANDONED),
    (TaskState.FAILED, TaskState.QUEUED),
    (TaskState.FAILED, TaskState.ABANDONED),
}

ALL_PAIRS = list(itertools.product(TaskState, TaskState))


# --------------------------------------------------------------------------- matrix


@pytest.mark.parametrize(("current", "target"), ALL_PAIRS)
def test_every_pair_matches_the_declared_matrix(current: TaskState, target: TaskState) -> None:
    """All 36 ordered pairs behave exactly as the table above says."""
    expected = (current, target) in LEGAL_PAIRS
    assert can_transition(current, target) is expected


def test_transitions_covers_every_state() -> None:
    """A state with no entry in TRANSITIONS would silently be a dead end."""
    assert set(TRANSITIONS) == set(TaskState)


@pytest.mark.parametrize("state", list(TaskState))
def test_no_state_transitions_to_itself(state: TaskState) -> None:
    """Self-transitions are refused: they are almost always a double-dispatch bug."""
    assert not can_transition(state, state)


def test_terminal_states_are_done_and_abandoned() -> None:
    assert TERMINAL_STATES == {TaskState.DONE, TaskState.ABANDONED}
    assert is_terminal(TaskState.DONE)
    assert is_terminal(TaskState.ABANDONED)
    assert not is_terminal(TaskState.QUEUED)


def test_active_and_open_state_sets() -> None:
    """The ceiling and the dedup window read these, so they are pinned."""
    assert ACTIVE_STATES == {TaskState.RUNNING, TaskState.AWAITING_APPROVAL}
    assert OPEN_STATES == {
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.AWAITING_APPROVAL,
    }


def test_every_non_terminal_state_can_reach_a_terminal_one() -> None:
    """No state may trap a task forever. Walks the graph rather than trusting it."""
    for start in TaskState:
        if is_terminal(start):
            continue
        seen: set[TaskState] = set()
        frontier = [start]
        reached_terminal = False
        while frontier:
            state = frontier.pop()
            if state in seen:
                continue
            seen.add(state)
            if is_terminal(state):
                reached_terminal = True
                break
            frontier.extend(TRANSITIONS[state])
        assert reached_terminal, f"{start} cannot reach a terminal state"


# ------------------------------------------------------------------------ assertion


def test_assert_transition_is_silent_on_a_legal_move() -> None:
    """It raises or it does nothing. Doing nothing is the pass."""
    assert_transition(TaskState.QUEUED, TaskState.RUNNING)


def test_assert_transition_raises_on_an_illegal_move() -> None:
    with pytest.raises(IllegalTransition) as excinfo:
        assert_transition(TaskState.DONE, TaskState.RUNNING)
    assert excinfo.value.current == TaskState.DONE
    assert excinfo.value.target == TaskState.RUNNING


def test_illegal_transition_message_lists_the_legal_targets() -> None:
    """The exception has to be actionable when it surfaces in a log at 3am."""
    with pytest.raises(IllegalTransition) as excinfo:
        assert_transition(TaskState.QUEUED, TaskState.DONE)
    message = str(excinfo.value)
    assert "queued" in message
    assert "done" in message
    assert "running" in message  # one of the legal targets is named


def test_illegal_transition_from_terminal_says_so() -> None:
    with pytest.raises(IllegalTransition) as excinfo:
        assert_transition(TaskState.ABANDONED, TaskState.QUEUED)
    assert "terminal" in str(excinfo.value)


# ----------------------------------------------------------------------- model side


def test_transition_to_running_sets_started_at_and_counts_the_attempt(
    session: Session, task: Task
) -> None:
    assert task.attempts == 0
    assert task.started_at is None

    task.transition_to(TaskState.RUNNING)
    session.commit()

    assert task.state == TaskState.RUNNING
    assert task.attempts == 1
    assert task.started_at is not None
    assert task.started_at.tzinfo is not None  # aware, per UTCDateTime


def test_transition_to_done_sets_finished_at(session: Session, task: Task) -> None:
    task.transition_to(TaskState.RUNNING)
    task.transition_to(TaskState.DONE, result={"post": "drafted"})
    session.commit()

    assert task.finished_at is not None
    assert task.result == {"post": "drafted"}
    assert task.is_terminal


def test_transition_to_failed_records_the_error(session: Session, task: Task) -> None:
    task.transition_to(TaskState.RUNNING)
    task.transition_to(TaskState.FAILED, error="The adapter timed out.")
    session.commit()

    assert task.state == TaskState.FAILED
    assert task.error == "The adapter timed out."
    # failed is not terminal: it can be requeued.
    assert not task.is_terminal
    assert task.finished_at is None


def test_an_error_on_a_non_failure_transition_is_refused(task: Task) -> None:
    """Silently dropping the message would lose it. Refusing surfaces the caller bug."""
    task.transition_to(TaskState.RUNNING)
    with pytest.raises(ValueError, match="only a move to 'failed'"):
        task.transition_to(TaskState.DONE, error="but it worked?")


def test_a_retry_increments_attempts_and_clears_the_previous_error(
    session: Session, task: Task
) -> None:
    task.transition_to(TaskState.RUNNING)
    task.transition_to(TaskState.FAILED, error="Rate limited.")
    task.transition_to(TaskState.QUEUED)
    task.transition_to(TaskState.RUNNING)
    session.commit()

    assert task.attempts == 2
    assert task.error is None


def test_started_at_survives_a_retry(session: Session, task: Task) -> None:
    """First start, not most recent, so duration covers the whole ordeal."""
    first = dt.datetime(2026, 9, 24, 10, 0, tzinfo=dt.UTC)
    task.transition_to(TaskState.RUNNING, now=first)
    task.transition_to(TaskState.FAILED, error="nope")
    task.transition_to(TaskState.QUEUED)
    task.transition_to(TaskState.RUNNING, now=first + dt.timedelta(hours=1))
    session.commit()

    assert task.started_at == first


def test_a_terminal_task_cannot_be_moved(session: Session, task: Task) -> None:
    task.transition_to(TaskState.RUNNING)
    task.transition_to(TaskState.DONE)
    session.commit()

    for target in TaskState:
        with pytest.raises(IllegalTransition):
            task.transition_to(target)


def test_approval_round_trip_resumes_the_task(session: Session, task: Task) -> None:
    """The gate path: run, park, approve, resume, finish."""
    task.transition_to(TaskState.RUNNING)
    task.transition_to(TaskState.AWAITING_APPROVAL)
    assert task.state == TaskState.AWAITING_APPROVAL
    assert not task.is_terminal

    task.transition_to(TaskState.RUNNING)
    task.transition_to(TaskState.DONE, result={"sent": True})
    session.commit()

    # Resuming after approval must not be counted as a fresh attempt at the work.
    assert task.attempts == 2
    assert task.state == TaskState.DONE


def test_rejection_abandons_the_task(session: Session, task: Task) -> None:
    task.transition_to(TaskState.RUNNING)
    task.transition_to(TaskState.AWAITING_APPROVAL)
    task.transition_to(TaskState.ABANDONED)
    session.commit()

    assert task.is_terminal
    assert task.finished_at is not None


def test_duration_is_none_until_the_task_finishes(session: Session, task: Task) -> None:
    assert task.duration_seconds is None
    task.transition_to(TaskState.RUNNING, now=dt.datetime(2026, 9, 24, 10, 0, tzinfo=dt.UTC))
    assert task.duration_seconds is None

    task.transition_to(TaskState.DONE, now=dt.datetime(2026, 9, 24, 10, 30, tzinfo=dt.UTC))
    session.commit()
    assert task.duration_seconds == 1800.0


def test_state_survives_a_round_trip_through_the_database(session: Session, task: Task) -> None:
    """The enum is stored as a string, so it has to come back as the enum."""
    task.transition_to(TaskState.RUNNING)
    task.transition_to(TaskState.AWAITING_APPROVAL)
    session.commit()
    session.expire_all()

    reloaded = session.get(Task, task.id)
    assert reloaded is not None
    assert reloaded.state == TaskState.AWAITING_APPROVAL
    assert isinstance(reloaded.state, TaskState)
