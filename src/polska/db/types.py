"""Column types shared across the models."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import DateTime, TypeDecorator
from sqlalchemy.engine import Dialect


class UTCDateTime(TypeDecorator[dt.datetime]):
    """A datetime that is always timezone-aware UTC on the way in and out.

    SQLite stores no timezone, so a plain ``DateTime(timezone=True)`` hands back naive
    datetimes and every comparison downstream becomes a coin toss. This normalises
    aware values to UTC before writing and re-attaches UTC on read.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "Naive datetime reached the database. Use polska.db.types.utcnow() "
                "or attach a timezone before assigning."
            )
        return value.astimezone(dt.UTC).replace(tzinfo=None)

    def process_result_value(
        self, value: dt.datetime | None, dialect: Dialect
    ) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)


def utcnow() -> dt.datetime:
    """Current time, timezone-aware, UTC. The only clock this project reads."""
    return dt.datetime.now(dt.UTC)


def utcday(moment: dt.datetime | None = None) -> str:
    """The UTC calendar day as ``YYYY-MM-DD``, used to key daily budget counters."""
    return (moment or utcnow()).astimezone(dt.UTC).strftime("%Y-%m-%d")


#: Convenience alias so models read as ``mapped_column(JSONDict)``.
JSONDict = dict[str, Any]
