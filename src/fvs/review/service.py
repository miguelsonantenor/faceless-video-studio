"""Review decisions, restoring versions, and the project list (PRJ-4, PRJ-5, QA-4, REV-3,
BAT-3, EXP-1).

- ``decide``: ready, approve, reject, skip or reopen, for one project or many. Marking
  ready or approving a render with a failed QA check needs ``override_qa`` and a note
  (QA-4); a rejection needs a note. Every decision is a ``reviews`` row. Approving
  writes the export package from the reviewed render.
- ``restore``: makes an earlier script or Edit Spec version current. Nothing is
  inserted; the pointers move and the state is worked out again from what exists.
- ``list``: projects by state, failed or needing attention, with the latest review note.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fvs.activity import ActivityLog, Source
from fvs.errors import ErrorCode, FvsError
from fvs.export.exporter import Exporter
from fvs.projects import STATE_LABELS, Projects, next_state, project_public
from fvs.spec.service import SpecService
from fvs.store import Database
from fvs.timeutil import now_iso

Decision = Literal["ready", "approve", "reject", "skip", "reopen"]
DECISIONS = ("ready", "approve", "reject", "skip", "reopen")
PAGE = 20
ATTENTION = ("changes_requested", "claude_check")
DONE_WORDS = {"ready": "marked ready for review", "approve": "approved and exported",
              "reject": "sent back with changes requested", "skip": "skipped",
              "reopen": "reopened"}


class ReviewService:
    def __init__(self, db: Database, *, projects: Projects, specs: SpecService,
                 exporter: Exporter, activity: ActivityLog) -> None:
        self.db = db
        self.projects = projects
        self.specs = specs
        self.exporter = exporter
        self.activity = activity

    # ---- decisions ---------------------------------------------------------------------

    def _render(self, proj: dict[str, Any]) -> dict[str, Any] | None:
        if not proj["render_id"]:
            return None
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM renders WHERE id = ?",
                               (proj["render_id"],)).fetchone()
        return dict(row) if row else None

    def _gate(self, proj: dict[str, Any], decision: str, override_qa: bool,
              note: str | None) -> dict[str, Any] | None:
        """The render a ready or approve applies to, after the QA rule (QA-4)."""
        if decision not in ("ready", "approve"):
            return None
        ref = f"#{proj['number']}"
        render = self._render(proj)
        if render is None or "render" in json.loads(proj["stale_flags"]):
            raise FvsError(ErrorCode.WRONG_STATE,
                           f"Project {ref} has no render of its current Edit Spec.",
                           f"Render it with fvs_render for {ref} first.")
        qa = json.loads(render["qa_report"]) if render["qa_report"] else None
        failed = [c for c in (qa or {}).get("checks", []) if c["status"] == "fail"]
        if failed and not override_qa:
            first = failed[0]
            raise FvsError(
                ErrorCode.QA_FAILED,
                f"{ref} failed {len(failed)} quality check(s): " + "; ".join(
                    f"{c['name']} ({c['measured']}, limit {c['limit']})" for c in failed),
                f"{first['fix'] or 'Fix it and render again.'} Or, if it is fine as it is, "
                "call again with override_qa true and a note saying why.",
                example=f'{{"projects": [{proj["number"]}], "decision": "{decision}", '
                        '"override_qa": true, "note": "the dark shot is intended"}')
        return render

    def _one(self, ref: int | str, decision: str, note: str | None, override_qa: bool,
             by: Source) -> dict[str, Any]:
        proj = self.projects.get_row(ref)
        render = self._gate(proj, decision, override_qa, note)
        export = None
        if decision == "approve":
            # Check the move first, so a refused approve writes no package.
            next_state(proj, "approve")
            export = self.exporter.export(proj, render or {})
        with self.db.transaction() as conn:
            row = self.projects.get_row(proj["id"], conn)
            fields: dict[str, Any] = {}
            if decision in ("approve", "reopen", "skip"):
                fields = {"failed": 0, "last_error": None}
            state = self.projects.advance(conn, row, decision, fields=fields)
            conn.execute(
                "INSERT INTO reviews (project_id, at, decision, note, by, qa_override, "
                "render_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (proj["id"], now_iso(), decision, note, by, int(override_qa),
                 render["id"] if render else proj["render_id"]))
        line = f"#{proj['number']} {DONE_WORDS[decision]}"
        if override_qa:
            line += " despite a failed quality check"
        if note:
            line += f": “{note}”"
        if export is not None:
            line += f" ({export.name})"
        self.activity.add(line + ".", source=by, project_id=proj["id"],
                          tool="fvs_project_review" if by == "claude" else None)
        out = {"project": proj["number"], "state": state, "state_label": STATE_LABELS[state]}
        if export is not None:
            out["export"] = str(export)
        return out

    def decide(self, projects: list[int | str], decision: Decision, *,
               note: str | None = None, override_qa: bool = False,
               by: Source = "claude") -> dict[str, Any]:
        note = (note or "").strip() or None
        if decision not in DECISIONS:
            raise FvsError(ErrorCode.SPEC_INVALID, f"Unknown decision {decision!r}.",
                           f"Use one of: {', '.join(DECISIONS)}.")
        if decision == "reject" and note is None:
            raise FvsError(ErrorCode.SPEC_INVALID, "A rejection needs a note saying what "
                           "to change.", "Add a note, e.g. \"hook too slow\".")
        if override_qa and note is None:
            raise FvsError(ErrorCode.SPEC_INVALID, "override_qa needs a note saying why "
                           "the failed check is acceptable (QA-4).",
                           "Add a note, e.g. \"the dark shot is intended\".")
        items = []
        for ref in projects:
            try:
                items.append(self._one(ref, decision, note, override_qa, by))
            except FvsError as exc:
                if len(projects) == 1:
                    raise
                items.append({"project": ref, "error": exc.to_dict()})
        ok = [i for i in items if "error" not in i]
        summary = (f"{len(ok)} project(s) {DONE_WORDS[decision]}"
                   + (f", {len(items) - len(ok)} refused" if len(ok) < len(items) else "")
                   + ".")
        return {"items": items, "summary": summary}

    def approve_passed(self, *, by: Source = "creator") -> dict[str, Any]:
        """Approve every project in review whose render passed every check (REV-3)."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT p.number, r.qa_report FROM projects p JOIN renders r "
                "ON r.id = p.render_id WHERE p.state = 'in_review' ORDER BY p.number"
            ).fetchall()
        passed = [r["number"] for r in rows
                  if r["qa_report"] and json.loads(r["qa_report"])["status"] == "pass"]
        if not passed:
            return {"items": [], "summary": "No project in review passed every check."}
        return self.decide(passed, "approve", by=by)

    # ---- restore (PRJ-5) ---------------------------------------------------------------

    def restore(self, project: int | str, *, script_version: int | None = None,
                spec_version: int | None = None, source: Source = "claude"
                ) -> dict[str, Any]:
        if (script_version is None) == (spec_version is None):
            raise FvsError(ErrorCode.SPEC_INVALID,
                           "Give exactly one of script_version or spec_version.",
                           "Call fvs_project_get to see the versions.",
                           example='{"project": 3, "spec_version": 2}')
        proj = self.projects.get_row(project)
        ref = f"#{proj['number']}"
        if proj["state"] in ("exported", "skipped"):
            raise FvsError(ErrorCode.WRONG_STATE, f"Project {ref} is "
                           f"{STATE_LABELS[proj['state']]}.",
                           f"Reopen {ref} first (fvs_project_review with decision 'reopen').")
        if script_version is not None:
            return self._restore_script(proj, script_version, source)
        return self._restore_spec(proj, spec_version or 0, source)

    def _restore_script(self, proj: dict[str, Any], version: int, source: Source
                        ) -> dict[str, Any]:
        with self.db.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM script_versions WHERE project_id = ? AND "
                                  "version = ?", (proj["id"], version)).fetchone()
            if exists is None:
                raise FvsError(ErrorCode.NOT_FOUND, f"Project #{proj['number']} has no "
                               f"script version {version}.",
                               "Call fvs_project_get to see the versions.")
            vo = conn.execute("SELECT id FROM voiceovers WHERE project_id = ? AND "
                              "script_version = ? ORDER BY created_at DESC LIMIT 1",
                              (proj["id"], version)).fetchone()
            stale = set(json.loads(proj["stale_flags"])) | {"render"}
            if vo is None:
                state, voiceover_id = "scripted", proj["voiceover_id"]
                stale.add("voiceover")
            else:
                voiceover_id = vo["id"]
                stale.discard("voiceover")
                state = "planned" if proj["spec_v"] else "voiced"
            conn.execute("UPDATE projects SET script_v = ?, voiceover_id = ?, state = ?, "
                         "stale_flags = ?, updated_at = ? WHERE id = ?",
                         (version, voiceover_id, state, json.dumps(sorted(stale)), now_iso(),
                          proj["id"]))
        note = "" if vo is not None else " It needs a new voiceover."
        summary = f"Restored script v{version} of #{proj['number']}.{note}"
        self.activity.add(summary, source=source, project_id=proj["id"],
                          tool="fvs_project_restore" if source == "claude" else None)
        return {"project": project_public(self.projects.get_row(proj["id"])),
                "summary": summary}

    def _restore_spec(self, proj: dict[str, Any], version: int, source: Source
                      ) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT spec_json, spec_hash FROM edit_specs WHERE "
                               "project_id = ? AND version = ?",
                               (proj["id"], version)).fetchone()
        if row is None:
            raise FvsError(ErrorCode.NOT_FOUND, f"Project #{proj['number']} has no Edit "
                           f"Spec version {version}.", "Call fvs_project_get to see the "
                           "versions.")
        self.specs.check(proj, json.loads(row["spec_json"]))  # still fits the voiceover?
        with self.db.transaction() as conn:
            render = conn.execute("SELECT id FROM renders WHERE spec_hash = ?",
                                  (row["spec_hash"],)).fetchone()
            stale = set(json.loads(proj["stale_flags"]))
            if render is not None:
                state, render_id = "claude_check", render["id"]
                stale.discard("render")
            else:
                state, render_id = "planned", proj["render_id"]
                stale.add("render")
            conn.execute("UPDATE projects SET spec_v = ?, render_id = ?, state = ?, "
                         "stale_flags = ?, updated_at = ? WHERE id = ?",
                         (version, render_id, state, json.dumps(sorted(stale)), now_iso(),
                          proj["id"]))
        summary = (f"Restored Edit Spec v{version} of #{proj['number']}"
                   + (" and its render." if render is not None else "; render it again."))
        self.activity.add(summary, source=source, project_id=proj["id"],
                          tool="fvs_project_restore" if source == "claude" else None)
        return {"project": project_public(self.projects.get_row(proj["id"])),
                "summary": summary}

    # ---- the list (BAT-3, REV-3) -------------------------------------------------------

    def list_projects(self, *, state: str | None = None, failed_only: bool = False,
             needs_attention: bool = False, cursor: int | None = None,
             concise: bool = True) -> dict[str, Any]:
        where, args = ["number > ?"], [cursor or 0]
        if state:
            if state not in STATE_LABELS:
                raise FvsError(ErrorCode.SPEC_INVALID, f"Unknown state {state!r}.",
                               f"Use one of: {', '.join(STATE_LABELS)}.")
            where.append("state = ?")
            args.append(state)
        if failed_only:
            where.append("failed = 1")
        if needs_attention:
            where.append(f"(failed = 1 OR state IN ({','.join('?' * len(ATTENTION))}))")
            args.extend(ATTENTION)
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM projects WHERE {' AND '.join(where)} ORDER BY number "
                "LIMIT ?", (*args, PAGE + 1))]
            counts = {r["state"]: r["n"] for r in conn.execute(
                "SELECT state, COUNT(*) AS n FROM projects GROUP BY state")}
            failed = conn.execute("SELECT COUNT(*) FROM projects WHERE failed = 1"
                                  ).fetchone()[0]
            notes = {r["project_id"]: dict(r) for r in conn.execute(
                "SELECT project_id, decision, note, at FROM reviews WHERE id IN "
                "(SELECT MAX(id) FROM reviews GROUP BY project_id)")}
            qa = {r["id"]: json.loads(r["qa_report"]) for r in conn.execute(
                "SELECT id, qa_report FROM renders WHERE qa_report IS NOT NULL")}
        more = len(rows) > PAGE
        items = []
        for r in rows[:PAGE]:
            item: dict[str, Any] = {"number": r["number"], "title": r["title"],
                                    "state": r["state"], "failed": bool(r["failed"]),
                                    "stale": json.loads(r["stale_flags"])}
            if r["failed"] and r["last_error"]:
                err = json.loads(r["last_error"])
                item["error"] = err if not concise else {"code": err["code"],
                                                         "message": err["message"]}
            review = notes.get(r["id"])
            if review and review["note"]:
                item["review_note"] = review["note"]
            report = qa.get(r["render_id"]) if r["render_id"] else None
            if report:
                item["qa"] = report["status"]
            if not concise:
                item.update(project_public(r))
            items.append(item)
        hint = ("More: call again with cursor set to next_cursor." if more else
                "Filter with state, failed_only or needs_attention.")
        by_state = ", ".join(f"{n} {STATE_LABELS[s].lower()}" for s, n in sorted(
            counts.items(), key=lambda kv: list(STATE_LABELS).index(kv[0])))
        return {"items": items, "next_cursor": rows[PAGE - 1]["number"] if more else None,
                "counts": counts, "failed": failed, "hint": hint,
                "summary": f"{sum(counts.values())} project(s): {by_state or 'none'}"
                           + (f"; {failed} failed" if failed else "") + "."}
