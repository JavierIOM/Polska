"""What a worker agent returns, and the actions it may ask for.

An agent never performs an external effect itself. It asks, by returning an
:class:`ActionRequest`, and the runner decides whether that executes now or stops at
the approval gate. This keeps the classification in one place instead of scattered
through four agent prompts.
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ActionRequest(BaseModel):
    """A proposed external side effect.

    ``payload`` must be complete. When this is approved days later, the stored payload
    is handed to the adapter exactly as it is here, and the agent is not consulted
    again. Anything the adapter needs that is missing from the payload is a bug that
    will only show up at execution time.
    """

    model_config = ConfigDict(extra="forbid")

    #: Dotted type, e.g. "email.send". Matched against the config classifier.
    action_type: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$")
    #: Which integration carries it out. "dry_run" is the only one that exists yet.
    adapter: str = Field(min_length=1, max_length=60)
    #: The complete, replayable call.
    payload: dict[str, Any] = Field(default_factory=dict)
    #: Human-readable rendering of exactly what the payload will do. This is what
    #: appears on the dashboard next to the approve button.
    preview: str = Field(min_length=1, max_length=20000)
    #: One line for the activity feed.
    summary: str = Field(min_length=1, max_length=300)

    @field_validator("payload")
    @classmethod
    def _no_credentials_in_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Stop an agent stuffing a key into a payload that gets written to disk.

        Approval payloads are persisted in SQLite and rendered on the dashboard, so a
        credential in one is a credential at rest in two more places.
        """
        suspicious = {"token", "api_key", "apikey", "secret", "password", "credential"}
        for name in value:
            if name.lower().replace("-", "_") in suspicious:
                raise ValueError(
                    f"Action payload contains {name!r}, which looks like a credential. "
                    "Adapters read their own secrets from the environment at call "
                    "time. Payloads are stored and displayed, so they must not carry one."
                )
        return value


class AgentResult(BaseModel):
    """What a worker agent hands back when its task is finished.

    ``succeeded`` is the agent's own verdict on whether it did the job. It is recorded,
    but the runner does not take it on trust: a result with no output and no actions
    still reads as a failure regardless of what the agent claims.
    """

    model_config = ConfigDict(extra="forbid")

    succeeded: bool
    #: One line for the feed and the dashboard row.
    summary: str = Field(min_length=1, max_length=500)
    #: The substance of the work. Shape varies by task type.
    output: dict[str, Any] = Field(default_factory=dict)
    #: External effects the agent wants performed. May be empty.
    actions: list[ActionRequest] = Field(default_factory=list, max_length=20)
    #: Set when ``succeeded`` is false and the work was attempted but did not
    #: come out right. Never set alongside ``blocked_reason``: those are two
    #: different claims about what happened, not two ways to phrase one.
    failure_reason: str = Field(default="", max_length=2000)
    #: Set when ``succeeded`` is false because this task cannot be executed in
    #: this environment at all -- no runtime to run a test, no way to verify a
    #: change -- distinct from a failed attempt. Found live: an engineer task
    #: with a genuinely narrow, single-file scope still burned most of its run
    #: ceiling because the container had no way to run the TypeScript it was
    #: asked to verify, and instead of reporting that, it copied the file out
    #: of the read-only clone and improvised a workaround. This field exists so
    #: the next one says "I cannot verify this here" instead. Retrying an
    #: environment-blocked task at the same scope cannot ever help, so it goes
    #: straight to TaskState.BLOCKED rather than through the ordinary
    #: attempts/cost retry path.
    blocked_reason: str = Field(default="", max_length=2000)
    #: Things the agent noticed but was not asked about. Fed to the next plan.
    observations: list[str] = Field(default_factory=list, max_length=20)
    #: How many edit-and-verify cycles the engineer actually used, set
    #: whether it stopped early because verification passed or because it
    #: exhausted its bound (see prompts.py's _VERIFY_LOOP_BOUND). Optional
    #: and unused by agents with no such loop (marketer, support, analyst):
    #: shared on this schema rather than forked per-agent because the outcome
    #: shape it supports, "tried N times, here is what happened each time",
    #: is the same self-reported claim as failure_reason, just structured
    #: enough to sanity-check against Run.tools_called after the fact rather
    #: than trusted on its own.
    iterations_attempted: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _outcome_is_explained_exactly_once(self) -> Self:
        failure_set = bool(self.failure_reason.strip())
        blocked_set = bool(self.blocked_reason.strip())

        if self.succeeded:
            if failure_set or blocked_set:
                raise ValueError(
                    "The agent reported success but also gave a failure_reason or "
                    "blocked_reason. Pick one: an ambiguous result would be recorded "
                    "as a success and the warning lost."
                )
            return self

        if failure_set and blocked_set:
            raise ValueError(
                "The agent set both failure_reason and blocked_reason. These are "
                "different claims (an attempt that didn't work, versus an "
                "environment that cannot run this task at all) and only one can "
                "be true. Pick the one that actually happened."
            )
        if not failure_set and not blocked_set:
            raise ValueError(
                "The agent reported failure without a failure_reason or "
                "blocked_reason. A task that failed for no stated reason cannot "
                "be retried intelligently, abandoned confidently, or recognised "
                "as unexecutable here."
            )
        return self

    @property
    def has_actions(self) -> bool:
        """True if this result wants something done to the outside world."""
        return bool(self.actions)

    @property
    def is_environment_blocked(self) -> bool:
        """True if the agent reported this task as unexecutable here, rather
        than attempted and failed."""
        return bool(self.blocked_reason.strip())
