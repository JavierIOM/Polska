"""One place to write to the activity feed, so every writer produces the same shape.

Nothing here queries the feed. It is write-only by design: reading it back is the
dashboard's job (``polska.dashboard.routes.home``), not this module's.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from polska.db.enums import ActivityKind
from polska.db.models import ActivityEvent


def log(
    session: Session,
    *,
    company_id: int | None,
    kind: ActivityKind,
    summary: str,
    detail: dict[str, Any] | None = None,
    task_id: int | None = None,
    run_id: int | None = None,
    approval_id: int | None = None,
    error: str | None = None,
) -> ActivityEvent:
    """Append one line to the feed and flush it immediately.

    Flushed rather than left for the caller's eventual commit, because the whole
    point of logging an external effect before attempting it is that the log survives
    even if the attempt itself then raises and rolls the rest of the transaction back.
    A flush assigns the row an id and makes it visible to any other reader in this
    transaction; only a hard crash between the flush and the eventual commit could
    still lose it, which is the same limit any single-database design has.
    """
    event = ActivityEvent(
        company_id=company_id,
        kind=kind,
        summary=summary,
        detail=detail,
        task_id=task_id,
        run_id=run_id,
        approval_id=approval_id,
        error=error,
    )
    session.add(event)
    session.flush()
    return event
