"""The activity feed: one plain-language line per tool call and job event (MCP-11, REV-1)."""

from __future__ import annotations

from typing import Any, Literal

from fvs.events import EventBus
from fvs.store import Database
from fvs.timeutil import now_iso

Source = Literal["claude", "creator", "engine"]


class ActivityLog:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus

    def add(
        self,
        summary: str,
        *,
        source: Source = "engine",
        project_id: str | None = None,
        tool: str | None = None,
    ) -> dict[str, Any]:
        at = now_iso()
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO activity (at, project_id, source, tool, summary) "
                "VALUES (?, ?, ?, ?, ?)",
                (at, project_id, source, tool, summary),
            )
            entry = {
                "id": cur.lastrowid,
                "at": at,
                "project_id": project_id,
                "source": source,
                "tool": tool,
                "summary": summary,
            }
        self.bus.publish({"type": "activity", "entry": entry})
        return entry

    def recent(self, limit: int = 50, before_id: int | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        with self.db.connect() as conn:
            if before_id is None:
                rows = conn.execute(
                    "SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM activity WHERE id < ? ORDER BY id DESC LIMIT ?",
                    (before_id, limit),
                ).fetchall()
        return [dict(r) for r in rows]
