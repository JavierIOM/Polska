"""The adapter interface and the one implementation phase 1 ships."""

from __future__ import annotations

import pytest

from polska.adapters.base import AdapterResult
from polska.adapters.dry_run import DryRunAdapter
from polska.adapters.registry import AdapterRegistry, UnknownAdapter


async def test_dry_run_reports_success_and_touches_nothing() -> None:
    adapter = DryRunAdapter()
    result = await adapter.execute("email.send", {"to": "someone@example.invalid"})
    assert result.succeeded
    assert result.detail["action_type"] == "email.send"
    assert result.detail["would_have_sent"] == {"to": "someone@example.invalid"}


async def test_dry_run_records_the_adapter_name_in_its_own_detail() -> None:
    adapter = DryRunAdapter()
    result = await adapter.execute("social.publish", {"text": "hello"})
    assert result.detail["adapter"] == "dry_run"


def test_adapter_result_defaults_to_no_detail_and_no_error() -> None:
    result = AdapterResult(succeeded=True)
    assert result.detail == {}
    assert result.error == ""


def test_registry_ships_with_dry_run_registered() -> None:
    registry = AdapterRegistry()
    assert "dry_run" in registry.names()
    adapter = registry.get("dry_run")
    assert isinstance(adapter, DryRunAdapter)


def test_registry_refuses_an_unknown_name() -> None:
    registry = AdapterRegistry()
    with pytest.raises(UnknownAdapter, match="github"):
        registry.get("github")


def test_registering_a_second_adapter_does_not_disturb_the_first() -> None:
    class FakeAdapter(DryRunAdapter):
        name = "fake"

    registry = AdapterRegistry()
    registry.register(FakeAdapter())
    assert set(registry.names()) == {"dry_run", "fake"}
    assert isinstance(registry.get("dry_run"), DryRunAdapter)
