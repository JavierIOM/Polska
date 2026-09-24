"""Database layer: engine, models, and the task state machine."""

from __future__ import annotations

from polska.db.base import Base, make_engine, make_session_factory, session_scope
from polska.db.state import (
    ACTIVE_STATES,
    DEDUP_LOOKBACK_STATES,
    DEDUP_NEVER_SUPPRESSES,
    DEDUP_SUPPRESSING_STATES,
    OPEN_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    assert_transition,
    can_transition,
    is_terminal,
)

__all__ = [
    "ACTIVE_STATES",
    "DEDUP_LOOKBACK_STATES",
    "DEDUP_NEVER_SUPPRESSES",
    "DEDUP_SUPPRESSING_STATES",
    "OPEN_STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "Base",
    "IllegalTransition",
    "assert_transition",
    "can_transition",
    "is_terminal",
    "make_engine",
    "make_session_factory",
    "session_scope",
]
