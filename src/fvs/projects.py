"""Projects and their lifecycle (PRJ-1–3, DS 7, BUILD_PLAN 4.1).

A project is one video to publish. Its state follows the 10-state lifecycle; every
service moves it through ``Projects.advance`` so the rules live in one place. Failure is
a flag (``failed``, ``last_error``), never a state. ``stale_flags`` lists the parts that
no longer match the current script or spec ("voiceover", "render").
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from fvs.activity import ActivityLog, Source
from fvs.errors import ErrorCode, FvsError
from fvs.library import Library, title_from_name
from fvs.timeutil import now_iso

STATE_LABELS = {"new": "New", "scripted": "Scripted", "voiced": "Voiced",
                "planned": "Planned", "rendering": "Rendering", "claude_check": "Claude check",
                "in_review": "In review", "changes_requested": "Changes requested",
                "exported": "Exported", "skipped": "Skipped"}

_WORKING = ("scripted", "voiced", "planned", "rendering", "claude_check", "in_review",
            "changes_requested")

# event -> {from state: to state}. A state missing from an event's map refuses the event.
TRANSITIONS: dict[str, dict[str, str]] = {
    # A new script from any state up to In review returns to Scripted (DS 7, going back).
    "script_saved": {s: "scripted" for s in ("new", *_WORKING)},
    # A new voiceover: Scripted → Voiced; once a spec exists the project needs a new
    # render, so later states return to Planned.
    "voiceover_made": {"scripted": "voiced", "voiced": "voiced",
                       **{s: "planned" for s in _WORKING[2:]}},
    "spec_saved": {s: "planned" for s in _WORKING[1:]},
    "render_started": {"planned": "rendering"},
    "render_done": {"rendering": "claude_check"},
    "ready": {"claude_check": "in_review"},
    "approve": {"in_review": "exported"},
    "reject": {"in_review": "changes_requested"},
    "skip": {"new": "skipped"},
    "reopen": {"skipped": "new", "exported": "planned"},
}

EVENT_WORDS = {"script_saved": "save a script", "voiceover_made": "make a voiceover",
               "spec_saved": "save an Edit Spec", "render_started": "start a render",
               "render_done": "finish a render", "ready": "mark it ready",
               "approve": "approve it", "reject": "reject it", "skip": "skip it",
               "reopen": "reopen it"}


def slugify(title: str, max_len: int = 50) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "video"


def next_state(project: dict[str, Any], event: str) -> str:
    """The state after ``event``, or WRONG_STATE explaining what to do instead."""
    state = project["state"]
    to = TRANSITIONS[event].get(state)
    if to is not None:
        return to
    ref = f"#{project['number']}"
    fix = {
        "exported": f"Reopen {ref} first (fvs_project_review with decision 'reopen').",
        "skipped": f"Reopen {ref} first (fvs_project_review with decision 'reopen').",
        "new": f"Save a script for {ref} first with fvs_script_save.",
        "scripted": f"Make the voiceover for {ref} first with fvs_voiceover_generate.",
        "voiced": f"Save an Edit Spec for {ref} first with fvs_edit_save.",
    }.get(state, f"Check {ref}'s state with fvs_project_get.")
    raise FvsError(ErrorCode.WRONG_STATE,
                   f"Project {ref} is {STATE_LABELS[state]}, so you cannot "
                   f"{EVENT_WORDS[event]} now.", fix)


def project_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "number": row["number"], "title": row["title"],
        "slug": row["slug"], "series": row["series"], "part": row["part"],
        "state": row["state"], "template": row["template"],
        "script_version": row["script_v"], "voiceover_id": row["voiceover_id"],
        "spec_version": row["spec_v"], "render_id": row["render_id"],
        "failed": bool(row["failed"]),
        "last_error": json.loads(row["last_error"]) if row["last_error"] else None,
        "stale": json.loads(row["stale_flags"]),
    }


class Projects:
    def __init__(self, db: Any, library: Library, activity: ActivityLog,
                 workspace: Path) -> None:
        self.db = db
        self.library = library
        self.activity = activity
        self.workspace = workspace

    def folder(self, project: dict[str, Any]) -> Path:
        """``workspace/projects/<NN>-<slug>`` (DS 6.2)."""
        return self.workspace / "projects" / f"{project['number']:02d}-{project['slug']}"

    def create(self, *, clips: list[int | str], title: str | None = None,
               series: str | None = None, part: int | None = None,
               template: str | None = None, source: Source = "claude") -> dict[str, Any]:
        """One project from one or more clips (PRJ-1, PRJ-2, PRJ-3)."""
        if not clips:
            raise FvsError(ErrorCode.NOT_FOUND, "A project needs at least one clip.",
                           "Pass the clip numbers from fvs_library_scan, e.g. clips=[3].")
        rows = [self.library.get_row(c) for c in clips]
        ids = list(dict.fromkeys(r["id"] for r in rows))
        title = (title or title_from_name(rows[0]["file_name"])).strip()
        at = now_iso()
        with self.db.transaction() as conn:
            number = conn.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM projects").fetchone()[0]
            pid = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO projects (id, number, title, slug, series, part, template, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, number, title, slugify(title), series, part, template, at, at))
            conn.executemany(
                "INSERT INTO project_clips (project_id, clip_id, position) VALUES (?, ?, ?)",
                [(pid, cid, i) for i, cid in enumerate(ids)])
            row = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone())
        clip_refs = ", ".join(f"#{r['number']}" for r in rows)
        self.activity.add(f"Created project #{number} “{title}” from clip {clip_refs}.",
                          source=source, project_id=pid,
                          tool="fvs_project_create" if source == "claude" else None)
        return project_public(row)

    def get_row(self, ref: int | str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """A project by number (3, "3", "#3") or ID."""
        text = str(ref).strip().lstrip("#")
        query, arg = (("SELECT * FROM projects WHERE number = ?", int(text))
                      if text.isdigit() else ("SELECT * FROM projects WHERE id = ?", text))
        if conn is not None:
            row = conn.execute(query, (arg,)).fetchone()
        else:
            with self.db.connect() as c:
                row = c.execute(query, (arg,)).fetchone()
        if row is None:
            raise FvsError(ErrorCode.NOT_FOUND, f"There is no project {ref!r}.",
                           "Call fvs_projects_list to see the projects and their numbers.")
        return dict(row)

    def clips(self, project_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT c.* FROM project_clips pc JOIN clips c ON c.id = pc.clip_id "
                "WHERE pc.project_id = ? ORDER BY pc.position", (project_id,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def advance(conn: sqlite3.Connection, project: dict[str, Any], event: str, *,
                fields: dict[str, Any] | None = None, stale_add: tuple[str, ...] = (),
                stale_remove: tuple[str, ...] = ()) -> str:
        """Move a project on ``event`` inside the caller's transaction; returns the state."""
        state = next_state(project, event)
        stale = set(json.loads(project["stale_flags"]))
        stale = (stale | set(stale_add)) - set(stale_remove)
        values = {"state": state, "stale_flags": json.dumps(sorted(stale)),
                  "updated_at": now_iso(), **(fields or {})}
        sets = ", ".join(f"{k} = ?" for k in values)
        conn.execute(f"UPDATE projects SET {sets} WHERE id = ?",
                     (*values.values(), project["id"]))
        return state
