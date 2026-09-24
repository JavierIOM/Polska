"""Integration adapters: the only code allowed to touch the outside world.

One interface, so a real GitHub, Buffer or Gmail adapter slots in later without the
runner or the approval gate changing. Phase 1 ships exactly one implementation: a
dry-run logger that records what it would have done and performs nothing.
"""

from __future__ import annotations

from polska.adapters.base import AdapterResult, IntegrationAdapter
from polska.adapters.dry_run import DryRunAdapter
from polska.adapters.registry import AdapterRegistry, UnknownAdapter

__all__ = [
    "AdapterRegistry",
    "AdapterResult",
    "DryRunAdapter",
    "IntegrationAdapter",
    "UnknownAdapter",
]
