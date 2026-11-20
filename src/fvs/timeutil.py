from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    """UTC time as ISO 8601 text with microseconds, so text order is time order."""
    return utcnow().isoformat(timespec="microseconds")
