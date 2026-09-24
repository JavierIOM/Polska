"""The one adapter phase 1 ships: it logs what it would have done, and does nothing.

Every real adapter (GitHub, Buffer, Gmail) will eventually replace this for its own
``adapter:`` name in a company profile. Until then, every action in the system,
reversible or approved-irreversible, ends up here, which is exactly what
``integrations.force_dry_run: true`` is for.
"""

from __future__ import annotations

import logging
from typing import Any

from polska.adapters.base import AdapterResult, IntegrationAdapter
from polska.db.types import utcnow

logger = logging.getLogger("polska.adapters.dry_run")


class DryRunAdapter(IntegrationAdapter):
    """Records the call and reports success. Touches nothing outside the process."""

    name = "dry_run"

    async def execute(self, action_type: str, payload: dict[str, Any]) -> AdapterResult:
        timestamp = utcnow().isoformat()
        logger.info("dry_run: would perform %s with payload=%r", action_type, payload)
        return AdapterResult(
            succeeded=True,
            detail={
                "adapter": self.name,
                "action_type": action_type,
                "would_have_sent": payload,
                "recorded_at": timestamp,
            },
        )
