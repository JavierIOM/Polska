"""One tick at a time, however it was started.

A tick can start from the scheduler job, from cron, or by hand (``polska-cli tick``).
``polska-cli tick`` begins by treating every run still marked ``running`` as orphaned,
so a second tick starting while another is in flight would price the first one's live
runs at their worst case and strand its tasks. The lock is a file under ``data/``,
which every process in the container shares.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path


def tick_lock_path(workspace_root: Path) -> Path:
    return Path(workspace_root).parent / ".polska-tick.lock"


@contextlib.contextmanager
def tick_lock(path: Path) -> Iterator[bool]:
    """Yield True if this process now holds the lock, False if another tick does.
    Never waits. Yields True where ``fcntl`` does not exist (Windows dev), since there
    is no second process to collide with there."""
    try:
        import fcntl
    except ImportError:
        yield True
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
