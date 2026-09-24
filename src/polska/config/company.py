"""The company YAML profile: schema and loader.

The profile is the human-written brief. It is validated on load rather than read
loosely, because a typo in ``brand_voice`` is survivable but a typo in a goal's target
quietly changes what the orchestrator spends money chasing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from polska.db.enums import GoalStatus

_SLUG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


class GoalSpec(BaseModel):
    """A goal as written in the YAML."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    metric: str = Field(min_length=1, max_length=120)
    target: float
    current: float = 0.0
    unit: str = "count"
    priority: int = 100
    status: GoalStatus = GoalStatus.ACTIVE

    @field_validator("target")
    @classmethod
    def _target_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError(
                "A goal target must be greater than zero. A target of zero is already "
                "met and gives the planner nothing to work towards."
            )
        return value


class IntegrationSpec(BaseModel):
    """A connected external service.

    ``adapter`` names the implementation to use. In phase 1 the only one that exists is
    ``dry_run``, which records what it would have done and performs nothing.

    No credentials here. Config names the environment variable holding the secret, and
    the adapter reads it at call time.
    """

    model_config = ConfigDict(extra="forbid")

    adapter: str = Field(min_length=1, max_length=60)
    enabled: bool = True
    #: Non-secret settings, e.g. a repo name or a channel id.
    options: dict[str, Any] = Field(default_factory=dict)
    #: Names of env vars this adapter needs, e.g. ["GITHUB_TOKEN"]. Names only.
    secret_env: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reject_inline_secrets(self) -> Self:
        """Catch a credential pasted into ``options`` before it reaches git."""
        suspicious = {"token", "key", "secret", "password", "apikey", "api_key"}
        for name in self.options:
            if name.lower().replace("-", "_") in suspicious:
                raise ValueError(
                    f"Integration option {name!r} looks like a credential. Secrets do "
                    "not go in the profile: name the environment variable in "
                    "'secret_env' and let the adapter read it at call time."
                )
        return self


class CompanyProfile(BaseModel):
    """The whole brief for one company."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    idea: str = Field(min_length=1)
    brand_voice: str = ""
    #: Hard rules the planner and every worker agent are told to respect.
    constraints: list[str] = Field(default_factory=list)
    goals: list[GoalSpec] = Field(default_factory=list)
    integrations: dict[str, IntegrationSpec] = Field(default_factory=dict)
    active: bool = True

    @field_validator("slug")
    @classmethod
    def _slug_is_tame(cls, value: str) -> str:
        if not set(value) <= _SLUG_CHARS:
            raise ValueError(
                f"Company slug {value!r} must be lowercase letters, digits and hyphens "
                "only. It is used in paths and URLs."
            )
        return value

    @model_validator(mode="after")
    def _goal_keys_unique(self) -> Self:
        seen: set[str] = set()
        for goal in self.goals:
            if goal.key in seen:
                raise ValueError(
                    f"Duplicate goal key {goal.key!r}. Keys identify a goal across "
                    "reloads, so two goals cannot share one."
                )
            seen.add(goal.key)
        return self

    @property
    def open_goals(self) -> list[GoalSpec]:
        """Goals the planner should still be working towards."""
        return [g for g in self.goals if g.status == GoalStatus.ACTIVE]


class LoadedProfile(BaseModel):
    """A validated profile plus where it came from and what it hashed to."""

    model_config = ConfigDict(extra="forbid")

    profile: CompanyProfile
    path: str
    raw: str
    sha256: str


def profile_hash(raw: str) -> str:
    """Stable hash of the raw YAML, used to spot edits on disk."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_company_profile(path: str | Path) -> LoadedProfile:
    """Read, parse and validate a company profile.

    Raises ``FileNotFoundError`` if it is missing, ``yaml.YAMLError`` if it will not
    parse, and ``pydantic.ValidationError`` if it parses but says something invalid.
    """
    file_path = Path(path)
    # Explicit encoding: Python 3.12 on Windows defaults to cp1252 and would mangle
    # any non-ASCII character in the brief.
    raw = file_path.read_text(encoding="utf-8")

    data = yaml.safe_load(raw)
    if data is None:
        raise ValueError(f"Company profile {file_path} is empty.")
    if not isinstance(data, dict):
        raise ValueError(
            f"Company profile {file_path} must be a YAML mapping, got {type(data).__name__}."
        )

    return LoadedProfile(
        profile=CompanyProfile.model_validate(data),
        path=str(file_path),
        raw=raw,
        sha256=profile_hash(raw),
    )


def discover_profiles(directory: str | Path) -> list[Path]:
    """Every ``.yaml`` and ``.yml`` file in ``directory``, sorted."""
    root = Path(directory)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.suffix in {".yaml", ".yml"})
