"""Saving Edit Specs (EDT-3, EDT-4, PRJ-5, NFR-M1).

- A save takes a full spec or a JSON Merge Patch (RFC 7386) of the current one.
- A template named in the spec supplies the defaults under it.
- The spec is validated (schema, then cue resolution and rules). A dry run returns the
  resolved timeline, or every problem, without saving.
- Each saved version gets ``spec_hash``: a hash of the canonical spec plus its inputs
  (clip contents, voiceover, music, caption style). The same hash is the same version,
  and in WP4 the same render.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from pydantic import ValidationError

from fvs.activity import ActivityLog, Source
from fvs.atomic import write_text_atomic
from fvs.errors import ErrorCode, FvsError
from fvs.projects import Projects, next_state
from fvs.script.service import ScriptService
from fvs.spec.models import SCHEMA_VERSION, EditSpec, migrate, schema_errors
from fvs.spec.resolve import Context, resolve
from fvs.spec.templates import Templates, deep_merge
from fvs.store import Database
from fvs.timeutil import now_iso
from fvs.voice.service import VoiceService

# Part of every spec hash: raise it when the compiler's output for the same spec changes.
ENGINE_VERSION = 2


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386: objects merge, ``null`` deletes, anything else replaces."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


class SpecService:
    def __init__(self, db: Database, *, projects: Projects, scripts: ScriptService,
                 voice: VoiceService, templates: Templates, activity: ActivityLog) -> None:
        self.db = db
        self.projects = projects
        self.scripts = scripts
        self.voice = voice
        self.templates = templates
        self.activity = activity

    # ---- reading ---------------------------------------------------------------------

    def current(self, proj: dict[str, Any]) -> dict[str, Any] | None:
        if not proj["spec_v"]:
            return None
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM edit_specs WHERE project_id = ? AND version = ?",
                               (proj["id"], proj["spec_v"])).fetchone()
        return {"version": row["version"], "spec_hash": row["spec_hash"],
                "spec": json.loads(row["spec_json"])}

    def context(self, proj: dict[str, Any]) -> Context:
        """Everything cue resolution needs to know about the project."""
        with self.db.connect() as conn:
            assets = [dict(r) for r in conn.execute(
                "SELECT * FROM assets WHERE kind = 'music'")]
        return Context(
            timings=self.voice.timings(proj),
            clips={c["number"]: c for c in self.projects.clips(proj["id"])},
            caption_styles=set(self.templates.caption_styles()),
            assets={**{a["name"]: a for a in assets}, **{a["id"]: a for a in assets}},
            script_hook=self.scripts.current(proj)["hook"])

    # ---- validating ------------------------------------------------------------------

    def check(self, proj: dict[str, Any], data: dict[str, Any]
              ) -> tuple[EditSpec, dict[str, Any], str, Context]:
        """(spec, resolved timeline, spec hash, context), or an FvsError with details."""
        data = migrate(data)
        project = str(data.get("project", proj["number"])).lstrip("#")
        if project not in (str(proj["number"]), proj["id"]):
            raise FvsError(ErrorCode.SPEC_INVALID,
                           f"This spec is for project {data['project']!r}, not "
                           f"#{proj['number']}.", f'Set "project": {proj["number"]}.')
        data["project"] = proj["number"]
        if data.get("template"):
            template = self.templates.get(data["template"])
            data = deep_merge(template["spec"], {**data, "template": template["ref"]})
        try:
            spec = EditSpec.model_validate(data)
        except ValidationError as exc:
            details = schema_errors(exc)
            raise FvsError(ErrorCode.SPEC_INVALID,
                           f"The Edit Spec does not match the schema; first, "
                           f"{details[0]['path']}: {details[0]['problem']}",
                           "Fix each problem in details. Nothing was saved.",
                           details=details) from None
        ctx = self.context(proj)
        timeline = resolve(spec, ctx)
        return spec, timeline, self._hash(spec, ctx, timeline), ctx

    def _hash(self, spec: EditSpec, ctx: Context, timeline: dict[str, Any]) -> str:
        used = sorted({s["clip"] for s in timeline["segments"]})
        music = spec.audio.music
        inputs = {
            "engine": ENGINE_VERSION,
            "voiceover": ctx.timings.get("voiceover_id"),
            "clips": {n: ctx.clips[n]["content_hash"] for n in used},
            "music": ({k: ctx.assets[music.asset][k] for k in ("id", "path")}
                      if music is not None else None),
            "caption_style": self.templates.caption_styles().get(spec.captions.style),
        }
        blob = json.dumps({"spec": spec.dump(), "inputs": inputs}, sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    # ---- saving ----------------------------------------------------------------------

    def save(self, project: int | str, *, spec: dict[str, Any] | None = None,
             patch: dict[str, Any] | None = None, dry_run: bool = False,
             source: Source = "claude") -> dict[str, Any]:
        proj = self.projects.get_row(project)
        if (spec is None) == (patch is None):
            raise FvsError(ErrorCode.SPEC_INVALID, "Give either a full spec or a patch.",
                           "Send spec={…} for a whole Edit Spec, or patch={…} to change "
                           "only some fields of the current one.",
                           example='{"patch": {"captions": {"max_words": 2}}}')
        if patch is not None:
            current = self.current(proj)
            if current is None:
                raise FvsError(ErrorCode.NOT_FOUND,
                               f"Project #{proj['number']} has no Edit Spec to patch yet.",
                               "Get a proposal with fvs_edit_plan and save it as a full spec.")
            data = merge_patch(current["spec"], patch)
        else:
            data = dict(spec or {})
        if not dry_run:
            next_state(proj, "spec_saved")

        if dry_run:
            try:
                parsed, timeline, spec_hash, _ = self.check(proj, data)
            except FvsError as exc:
                return {"dry_run": True, "valid": False, "error": exc.to_dict(),
                        "errors": exc.details,
                        "summary": f"Dry run: the Edit Spec for #{proj['number']} has "
                                   f"problems: {exc.message}"}
            return {"dry_run": True, "valid": True, "spec_hash": spec_hash,
                    "spec": parsed.dump(), "timeline": timeline,
                    "warnings": timeline["warnings"],
                    "summary": f"Dry run: the Edit Spec for #{proj['number']} is valid, "
                               f"{len(timeline['segments'])} segment(s), "
                               f"{timeline['duration_s']:.1f} s. Nothing was saved."}

        parsed, timeline, spec_hash, _ = self.check(proj, data)
        canonical = parsed.dump()
        with self.db.transaction() as conn:
            proj = self.projects.get_row(proj["id"], conn)
            current_hash = conn.execute(
                "SELECT spec_hash FROM edit_specs WHERE project_id = ? AND version = ?",
                (proj["id"], proj["spec_v"])).fetchone() if proj["spec_v"] else None
            unchanged = current_hash is not None and current_hash[0] == spec_hash
            version = proj["spec_v"]
            if not unchanged:
                version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM edit_specs "
                                       "WHERE project_id = ?", (proj["id"],)).fetchone()[0]
                conn.execute(
                    "INSERT INTO edit_specs (id, project_id, version, spec_json, spec_hash, "
                    "schema_version, template_version, author, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, proj["id"], version,
                     json.dumps(canonical, ensure_ascii=False), spec_hash, SCHEMA_VERSION,
                     parsed.template, "creator" if source == "creator" else "claude",
                     now_iso()))
                write_text_atomic(self.projects.folder(proj) / "specs" / f"v{version}.json",
                                  json.dumps(canonical, ensure_ascii=False, indent=1))
                self.projects.advance(conn, proj, "spec_saved", fields={"spec_v": version},
                                      stale_add=("render",) if proj["render_id"] else ())
        n = len(timeline["segments"])
        summary = (f"Edit Spec v{version} for #{proj['number']} is unchanged." if unchanged
                   else f"Saved Edit Spec v{version} for #{proj['number']}: {n} segment(s), "
                        f"{timeline['duration_s']:.1f} s.")
        if not unchanged:
            self.activity.add(summary, source=source, project_id=proj["id"],
                              tool="fvs_edit_save" if source == "claude" else None)
        return {"project": proj["number"], "version": version, "spec_hash": spec_hash,
                "unchanged": unchanged, "timeline": timeline,
                "warnings": timeline["warnings"],
                "state": self.projects.get_row(proj["id"])["state"], "summary": summary}
