"""One tick at a time: a second tick must not start while one is in flight."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from polska.ticklock import tick_lock, tick_lock_path


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl locks are POSIX only")
def test_a_second_tick_cannot_take_the_lock_while_the_first_holds_it(tmp_path: Path) -> None:
    path = tick_lock_path(tmp_path / "workspaces")
    with tick_lock(path) as first:
        assert first is True
        with tick_lock(path) as second:
            assert second is False
    with tick_lock(path) as after_release:
        assert after_release is True


def test_the_lock_lives_beside_the_workspaces(tmp_path: Path) -> None:
    assert (
        tick_lock_path(tmp_path / "data" / "workspaces") == tmp_path / "data" / ".polska-tick.lock"
    )
