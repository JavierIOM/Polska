"""ProposedTask.sub_units: the schema half of the disguised-multi-unit-task
fix. See test_orchestrator.py for the deterministic-split half.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from polska.schemas.planner import ProposedTask, SubUnit


def _task(**overrides):
    base = {
        "type": "research",
        "title": "Map all six upstream sources",
        "description": "Document every silent-failure path.",
        "goal_key": "upstream-monitoring",
        "rationale": "Six sources, none audited yet.",
    }
    base.update(overrides)
    return ProposedTask(**base)


def test_sub_units_defaults_to_empty() -> None:
    task = _task()
    assert task.sub_units == []


def test_two_or_more_sub_units_is_a_valid_split() -> None:
    task = _task(
        sub_units=[
            {"title": "DVLA silent-failure signal", "description": "Just DVLA."},
            {"title": "MOT silent-failure signal", "description": "Just MOT."},
        ]
    )
    assert len(task.sub_units) == 2
    assert task.sub_units[0].title == "DVLA silent-failure signal"


def test_exactly_one_sub_unit_is_rejected() -> None:
    """A single entry is not a split -- it just means the proposal should
    have described the whole thing directly and left sub_units empty."""
    with pytest.raises(ValidationError, match="not a split"):
        _task(sub_units=[{"title": "Just DVLA signal", "description": "Just DVLA."}])


def test_a_placeholder_sub_unit_title_is_rejected() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        SubUnit(title="Untitled", description="Something.")
