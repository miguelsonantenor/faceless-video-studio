"""The daily character budget and the cost ledger (VO-6, NFR-C1, DS 8.5).

The limit is read from settings on every call, so a change applies to the very next
voice call, even inside a running batch. Today's spending is summed from the ledger
since the last ``voice.budget_reset_time`` (local time).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from fvs.settings import SettingsService
from fvs.store import Database
from fvs.timeutil import now_iso, utcnow


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


class Budget:
    def __init__(self, db: Database, settings: SettingsService) -> None:
        self.db = db
        self.settings = settings

    def window_start(self, now: datetime | None = None) -> datetime:
        """When today's budget day began, in local time."""
        local_now = (now or utcnow()).astimezone()
        hour, minute = map(int, self.settings.get().voice.budget_reset_time.split(":"))
        start = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return start if start <= local_now else start - timedelta(days=1)

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        start = self.window_start(now)
        with self.db.connect() as conn:
            spent = conn.execute("SELECT COALESCE(SUM(chars), 0) FROM cost_ledger "
                                 "WHERE at >= ?", (_iso(start),)).fetchone()[0]
        limit = self.settings.get().voice.daily_budget_chars
        return {"limit": limit, "spent_today": spent, "left": max(0, limit - spent),
                "resets_at": _iso(start + timedelta(days=1))}

    @staticmethod
    def record(conn: sqlite3.Connection, *, chars: int, cache_key: str | None,
               project_id: str | None, over_budget_approved: bool) -> None:
        conn.execute("INSERT INTO cost_ledger (at, chars, cache_key, project_id, "
                     "over_budget_approved) VALUES (?, ?, ?, ?, ?)",
                     (now_iso(), chars, cache_key, project_id, int(over_budget_approved)))
