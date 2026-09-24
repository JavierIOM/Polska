"""Looks up an adapter instance by the name a config or profile gives it.

The one indirection that lets a real GitHub adapter replace ``dry_run`` for the
``github:`` integration in a company profile without the gate or the runner
knowing anything changed: they ask the registry for "github" and get back whatever
is registered under that name.
"""

from __future__ import annotations

from polska.adapters.base import IntegrationAdapter
from polska.adapters.dry_run import DryRunAdapter


class UnknownAdapter(KeyError):
    """Raised when config or a profile names an adapter nothing has registered."""

    def __init__(self, name: str, known: list[str]) -> None:
        self.name = name
        super().__init__(
            f"No adapter registered under {name!r}. Known adapters: {sorted(known)}. "
            "A company profile or config named one that does not exist."
        )


class AdapterRegistry:
    """Holds one instance per adapter name.

    Ships pre-populated with ``dry_run``. Real adapters register themselves here as
    they are built, in later phases, without this class changing.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, IntegrationAdapter] = {}
        self.register(DryRunAdapter())

    def register(self, adapter: IntegrationAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> IntegrationAdapter:
        try:
            return self._adapters[name]
        except KeyError:
            raise UnknownAdapter(name, list(self._adapters)) from None

    def names(self) -> list[str]:
        return sorted(self._adapters)
