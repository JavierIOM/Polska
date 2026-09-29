"""What the planning agent is allowed to return.

The planner's output decides what the company does next, so it is validated before a
single field is read. Nothing in this project string-parses an agent response.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from polska.db.enums import TaskType

_PLACEHOLDER_TITLES = {"task", "todo", "tbd", "n/a", "none", "untitled"}


def _reject_placeholder_title(value: str) -> str:
    stripped = value.strip()
    if stripped.lower() in _PLACEHOLDER_TITLES:
        raise ValueError(
            f"Task title {value!r} is a placeholder. A task needs a title that "
            "says what it is, because the title is what dedup matches on."
        )
    return stripped


class SubUnit(BaseModel):
    """One independently-completable piece of a proposal that would otherwise
    bundle several unrelated units of work into a single task.

    Written by the planner itself, not derived by templating the parent's
    title/description: the planner already has the context to scope each
    unit correctly (which file, which source, which signal), and generating
    N variations of one description mechanically would just produce N tasks
    that all look like the same task with a different label stapled on,
    which defeats the point of splitting them at all.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=4, max_length=300)
    description: str = Field(min_length=1, max_length=4000)

    @field_validator("title")
    @classmethod
    def _title_is_not_a_placeholder(cls, value: str) -> str:
        return _reject_placeholder_title(value)


class ProposedTask(BaseModel):
    """One piece of work the planner wants done."""

    model_config = ConfigDict(extra="forbid")

    type: TaskType
    title: str = Field(min_length=4, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    #: The ``key`` of the goal this serves, as written in the company profile.
    goal_key: str = Field(min_length=1, max_length=64)
    #: Why this, why now. Shown on the dashboard beside the task.
    rationale: str = Field(min_length=1, max_length=2000)
    #: 1 is most urgent, 200 is filler. The planner is told the scale.
    priority: int = Field(default=100, ge=1, le=200)
    #: Set only when this proposal would otherwise bundle several independent
    #: units of work (e.g. "detect the silent-failure mode of all 6 upstream
    #: sources") into one task. Each entry becomes its own Task at enqueue
    #: time -- own dedup match, own budget ceiling, own verify-loop attempt --
    #: instead of one oversized task that can only fail as a whole. Empty is
    #: the normal case: most proposals are already one coherent unit.
    sub_units: list[SubUnit] = Field(default_factory=list, max_length=20)

    @field_validator("title")
    @classmethod
    def _title_is_not_a_placeholder(cls, value: str) -> str:
        return _reject_placeholder_title(value)

    @field_validator("sub_units")
    @classmethod
    def _sub_units_are_a_real_split(cls, value: list[SubUnit]) -> list[SubUnit]:
        if len(value) == 1:
            raise ValueError(
                "sub_units has exactly one entry. A single unit is not a split: "
                "leave sub_units empty and describe the whole task as usual."
            )
        return value


class PlannerOutput(BaseModel):
    """The planner's whole response.

    An empty ``tasks`` list is a valid and expected answer. A company with nothing
    worth doing this cycle should do nothing, and the planner is told so explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    tasks: list[ProposedTask] = Field(default_factory=list, max_length=50)
    #: The planner's read of where things stand. Recorded, not acted on.
    assessment: str = Field(default="", max_length=4000)
    #: Set when the planner deliberately proposes nothing, explaining why.
    no_action_reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _silence_is_explained(self) -> Self:
        """An empty plan has to say why, so a broken planner is distinguishable
        from a quiet one."""
        if not self.tasks and not self.no_action_reason.strip():
            raise ValueError(
                "The planner returned no tasks and no no_action_reason. An empty plan "
                "is allowed, but it must explain itself: an unexplained empty list is "
                "indistinguishable from a planner that has failed."
            )
        return self

    @property
    def is_empty(self) -> bool:
        """True if this cycle proposes no work."""
        return not self.tasks


class DedupVerdict(BaseModel):
    """The judge's call on one ambiguous proposal."""

    model_config = ConfigDict(extra="forbid")

    #: Index into the batch the judge was given.
    index: int = Field(ge=0)
    is_duplicate: bool
    #: Id of the task it duplicates, when it is one.
    duplicate_of_task_id: int | None = None
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _duplicates_name_their_twin(self) -> Self:
        if self.is_duplicate and self.duplicate_of_task_id is None:
            raise ValueError(
                f"Proposal {self.index} was judged a duplicate but no "
                "duplicate_of_task_id was given. A duplicate of nothing is not a "
                "duplicate, and the dashboard needs the link to show why it was dropped."
            )
        if not self.is_duplicate and self.duplicate_of_task_id is not None:
            raise ValueError(
                f"Proposal {self.index} was judged novel but still names "
                f"duplicate_of_task_id={self.duplicate_of_task_id}. Contradictory."
            )
        return self


class DedupJudgeOutput(BaseModel):
    """The judge's response for a whole batch."""

    model_config = ConfigDict(extra="forbid")

    verdicts: list[DedupVerdict] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _one_verdict_per_index(self) -> Self:
        seen: set[int] = set()
        for verdict in self.verdicts:
            if verdict.index in seen:
                raise ValueError(f"The judge returned two verdicts for index {verdict.index}.")
            seen.add(verdict.index)
        return self
