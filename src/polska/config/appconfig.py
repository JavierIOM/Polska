"""The non-secret tunables, loaded from ``config/default.yaml`` and validated.

Nothing here is a credential. This file is tracked, read by both the orchestrator and
the dashboard, and is the single place a ceiling or a model tier is set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from polska.db.enums import AgentName


class SchedulerConfig(BaseModel):
    """When the orchestrator wakes up."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_hours: float = Field(default=4.0, gt=0)
    #: Random spread either side of the interval, so ticks do not align with anything.
    jitter_seconds: int = Field(default=300, ge=0)
    #: Run a planning cycle immediately on startup instead of waiting a full interval.
    run_on_start: bool = False
    #: If a tick is still running when the next is due, skip rather than stack up.
    coalesce: bool = True


class LimitsConfig(BaseModel):
    """How much work may be in flight."""

    model_config = ConfigDict(extra="forbid")

    max_concurrent_tasks: int = Field(default=2, ge=1)
    max_tasks_per_day: int = Field(default=12, ge=1)
    #: Ceiling on what one planning cycle may enqueue, before dedup.
    max_tasks_per_tick: int = Field(default=5, ge=1)
    #: A task that has failed this many times is abandoned rather than requeued.
    max_attempts: int = Field(default=3, ge=1)
    task_timeout_seconds: int = Field(default=900, ge=30)


class BudgetConfig(BaseModel):
    """Hard ceilings. Crossing one stops the scheduler and writes a BudgetHalt.

    There is no soft mode and no degraded mode. A ceiling is a stop.

    The ledger the guard enforces against is denominated in dollars, not tokens.
    Tokens are not a fungible unit across models: an Opus token and a Haiku token
    do not cost the same, so summing raw counts across every agent's runs would make
    a day-or-company ceiling meaningless the moment two different models are in
    play, which they are from the shipped config onwards. Dollars, computed from the
    same pricing table that already prices every run, are the only unit that adds up
    correctly across models.

    ``max_tokens_per_run`` survives as a same-model, single-run safety net: it bounds
    one call's raw output size, which is a real thing worth capping independently of
    price (a pricing-table mistake should not also mean unbounded output). It is
    never used to derive a dollar figure. ``max_usd_per_run`` is the actual per-run
    cost ceiling, enforced two ways: passed to the SDK as ``max_budget_usd`` so the
    CLI stops itself mid-run, and reserved in full against the day and company
    ledgers before the run starts, since that reservation, not a token estimate, is
    what the SDK is contracted to hold the run to.
    """

    model_config = ConfigDict(extra="forbid")

    max_tokens_per_run: int = Field(default=200_000, ge=1)
    max_usd_per_run: float = Field(default=3.0, gt=0)
    max_usd_per_day: float = Field(default=10.0, gt=0)
    max_usd_per_company: float = Field(default=250.0, gt=0)
    #: Frozen into each run row so a later rate change cannot rewrite old costs.
    usd_to_gbp: float = Field(default=0.79, gt=0)

    @model_validator(mode="after")
    def _run_within_day_within_company(self) -> Self:
        if self.max_usd_per_day > self.max_usd_per_company:
            raise ValueError(
                "max_usd_per_day is above max_usd_per_company, so the lifetime "
                "ceiling would be hit inside a single day and the daily one could "
                "never fire."
            )
        if self.max_usd_per_run > self.max_usd_per_day:
            raise ValueError(
                "max_usd_per_run is above max_usd_per_day. One run would exhaust the day."
            )
        return self


class DedupConfig(BaseModel):
    """Stopping the planner reproposing work it already did."""

    model_config = ConfigDict(extra="forbid")

    lookback_days: int = Field(default=14, ge=1)
    #: At or above this similarity, a proposal is a duplicate. No judge call.
    high_threshold: int = Field(default=90, ge=0, le=100)
    #: At or below this, it is novel. No judge call.
    low_threshold: int = Field(default=60, ge=0, le=100)
    #: Only the band between the two thresholds costs a model call.
    judge_model: str = "claude-haiku-4-5"
    judge_enabled: bool = True

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> Self:
        if self.low_threshold >= self.high_threshold:
            raise ValueError(
                f"dedup low_threshold ({self.low_threshold}) must be below "
                f"high_threshold ({self.high_threshold}), or there is no band left "
                "for the judge to decide."
            )
        return self


class AgentConfig(BaseModel):
    """One agent's model, tools and ceilings.

    ``tools`` is an allowlist and nothing else is granted. An empty list means the
    agent reasons and reports but touches nothing.
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    tools: list[str] = Field(default_factory=list)
    max_turns: int = Field(default=20, ge=1)
    timeout_seconds: int = Field(default=600, ge=30)
    enabled: bool = True
    #: Extra text appended to the agent's built-in system prompt.
    system_prompt_extra: str = ""


class ApprovalConfig(BaseModel):
    """The gate.

    ``irreversible_actions`` is the classifier's list. Anything matching lands in the
    approvals table and stops. ``auto_approve`` is the escape hatch and it ships empty:
    a populated one is a deliberate decision, not a default.
    """

    model_config = ConfigDict(extra="forbid")

    irreversible_actions: list[str] = Field(
        default_factory=lambda: [
            "email.send",
            "social.publish",
            "git.push_default_branch",
            "payment.charge",
            "contact.third_party",
        ]
    )
    auto_approve: list[str] = Field(default_factory=list)
    expiry_hours: int = Field(default=72, ge=1)
    #: An unknown action type is treated as irreversible. Fail closed.
    unknown_action_is_irreversible: bool = True

    @model_validator(mode="after")
    def _auto_approve_is_a_subset(self) -> Self:
        unknown = set(self.auto_approve) - set(self.irreversible_actions)
        if unknown:
            raise ValueError(
                f"auto_approve lists {sorted(unknown)}, which are not in "
                "irreversible_actions. Reversible actions execute directly and never "
                "reach the gate, so listing one here does nothing and hides intent."
            )
        return self


class ModelPricing(BaseModel):
    """USD per million tokens for one model."""

    model_config = ConfigDict(extra="forbid")

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    cache_write: float = Field(ge=0)

    def cost_usd(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> float:
        """Cost in USD for one run's usage."""
        per_million = 1_000_000
        return (
            input_tokens * self.input
            + output_tokens * self.output
            + cache_read_tokens * self.cache_read
            + cache_creation_tokens * self.cache_write
        ) / per_million


class IntegrationsConfig(BaseModel):
    """Which adapter is used when a profile does not name one."""

    model_config = ConfigDict(extra="forbid")

    default_adapter: str = "dry_run"
    #: A global off switch. With this true, even an approved action only logs.
    force_dry_run: bool = True


class AppConfig(BaseModel):
    """Everything in ``config/default.yaml``."""

    model_config = ConfigDict(extra="forbid")

    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    approvals: ApprovalConfig = Field(default_factory=ApprovalConfig)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)
    agents: dict[AgentName, AgentConfig]
    pricing: dict[str, ModelPricing]

    @field_validator("agents")
    @classmethod
    def _all_agents_present(
        cls, value: dict[AgentName, AgentConfig]
    ) -> dict[AgentName, AgentConfig]:
        missing = set(AgentName) - set(value)
        if missing:
            raise ValueError(
                f"No config for agent(s): {sorted(m.value for m in missing)}. "
                "Every agent needs an explicit model and tool allowlist, including "
                "an empty one."
            )
        return value

    @model_validator(mode="after")
    def _every_model_is_priced(self) -> Self:
        """A model with no price cannot be budgeted, so it cannot be used."""
        used = {agent.model for agent in self.agents.values()}
        if self.dedup.judge_enabled:
            used.add(self.dedup.judge_model)
        unpriced = sorted(used - set(self.pricing))
        if unpriced:
            raise ValueError(
                f"Model(s) {unpriced} are configured for an agent but have no entry "
                "under 'pricing'. The budget guard cannot cost a run it has no rate "
                "for, so this is refused rather than assumed to be free."
            )
        return self

    def price_for(self, model: str) -> ModelPricing:
        """Pricing for ``model``, or a clear failure."""
        try:
            return self.pricing[model]
        except KeyError:
            raise KeyError(
                f"No pricing configured for model {model!r}. Add it under 'pricing' "
                "in config/default.yaml."
            ) from None

    def is_irreversible(self, action_type: str) -> bool:
        """True if ``action_type`` must go through the approval gate.

        An action type nobody has classified is treated as irreversible when
        ``unknown_action_is_irreversible`` is set, which is the default. A new agent
        inventing an action name should stop at the gate, not sail through it.
        """
        if action_type in self.approvals.irreversible_actions:
            return True
        return self.approvals.unknown_action_is_irreversible

    def may_auto_approve(self, action_type: str) -> bool:
        """True if config permits this action type to skip a human decision."""
        return action_type in self.approvals.auto_approve


def load_app_config(path: str | Path) -> AppConfig:
    """Read and validate the orchestrator config."""
    file_path = Path(path)
    raw = file_path.read_text(encoding="utf-8")
    data: Any = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Config {file_path} must be a YAML mapping.")
    return AppConfig.model_validate(data)
