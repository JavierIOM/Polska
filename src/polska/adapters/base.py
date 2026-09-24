"""The adapter interface every integration implements.

An adapter's job is narrow on purpose: given an already-classified, already-stored
action payload, perform it and report what happened. It does not classify
reversibility (the gate does that, from config, before an adapter is ever called),
and it does not decide whether to run (the gate does that too). By the time
``execute`` is called, that decision is already made and logged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AdapterResult(BaseModel):
    """What an adapter hands back after attempting one action.

    Stored on ``Approval.execution_result`` (or, for a reversible action executed
    without ever going through the approval table, into the activity log directly).
    """

    model_config = ConfigDict(extra="forbid")

    succeeded: bool
    #: What actually happened, in the adapter's own terms. Free-form because each
    #: adapter's idea of a useful receipt differs (a message id, a commit sha, a
    #: post url).
    detail: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class IntegrationAdapter(ABC):
    """Base class for every integration.

    ``name`` is what company profiles and config name in ``adapter:``. It must be
    unique across every adapter registered, which :class:`AdapterRegistry` enforces.
    """

    name: str

    @abstractmethod
    async def execute(self, action_type: str, payload: dict[str, Any]) -> AdapterResult:
        """Perform ``action_type`` with ``payload`` and report what happened.

        ``payload`` is exactly what was stored on the :class:`Approval` (or, for a
        reversible action, exactly what the agent proposed). This method must not
        need anything that is not in it: by the time it is called, the agent that
        proposed the action is not being asked again.

        Must not raise for an ordinary failure (a bad address, a rejected API call).
        Return ``AdapterResult(succeeded=False, error=...)`` instead, so the caller
        can record the failure rather than crash the run that is doing the recording.
        An exception here means the adapter itself is broken, not that the action
        failed, and callers are entitled to treat it that way.
        """
        raise NotImplementedError
