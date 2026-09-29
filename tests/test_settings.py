"""Settings.resolved_agent_cli_wrapper_path: unset is fine, set-but-missing is not.

The distinction matters. Unset means "this concept doesn't apply here" (dev on
a machine with no second UID, no setpriv, no iptables rule). Set-but-missing
means something that was supposed to force every agent through a privilege
drop silently didn't, which must stop the process rather than run every agent
unrestricted while believing otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from polska.config.settings import Settings


def test_an_unset_wrapper_path_resolves_to_none() -> None:
    settings = Settings(agent_cli_wrapper_path=None)
    assert settings.resolved_agent_cli_wrapper_path() is None


def test_a_wrapper_path_that_exists_resolves_to_its_string_form(tmp_path: Path) -> None:
    wrapper = tmp_path / "polska-agent-cli"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")

    settings = Settings(agent_cli_wrapper_path=wrapper)
    assert settings.resolved_agent_cli_wrapper_path() == str(wrapper)


def test_a_configured_but_missing_wrapper_path_refuses_to_start(tmp_path: Path) -> None:
    settings = Settings(agent_cli_wrapper_path=tmp_path / "does-not-exist")
    with pytest.raises(RuntimeError, match="does not exist"):
        settings.resolved_agent_cli_wrapper_path()
