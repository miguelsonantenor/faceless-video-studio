"""The P1 tools (FS 5.2): thin wrappers over the services, with descriptions written for
Claude (DS 8.7): when to use the tool, what it costs, and what to do on each error.

Tools take a project or clip as its number (3, "#3") or its ID.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from fvs.analyze.analyzer import EXTRAS
from fvs.analyze.service import describe
from fvs.core import Core
from fvs.errors import FvsError
from fvs.jobs import ACTIVE_STATES
from fvs.library import PAGE_SIZE
from fvs.mcp.guide import MARKUP, WORKFLOW
from fvs.mcp.registry import Args, Progress, Registry, Tool
from fvs.projects import STATE_LABELS, project_public
from fvs.see.service import Seen
from fvs.spec.models import EditSpec

Ref = int | str
Format = Literal["concise", "detailed"]
JOB_WAIT_MAX_S = 15.0
GUIDE_SECTIONS = ("workflow", "markup", "edit_spec_schema", "templates", "caption_styles",
                  "voices", "budget", "settings")
PROJECT_PARTS = ("script", "timings", "spec", "timeline", "renders", "reviews", "history")


def _ref_field(what: str) -> Any:
    return Field(description=f"{what} number (e.g. 3) or ID")


# ---- fvs_guide --------------------------------------------------------------------------


class GuideArgs(Args):
    sections: list[Literal[GUIDE_SECTIONS]] | None = Field(
        None, description="Only these parts; default all")


def guide(core: Core, a: GuideArgs) -> dict[str, Any]:
    want = set(a.sections or GUIDE_SECTIONS)
    out: dict[str, Any] = {}
    if "workflow" in want:
        out["workflow"] = WORKFLOW
    if "markup" in want:
        out["markup"] = MARKUP
    if "edit_spec_schema" in want:
        out["edit_spec_schema"] = EditSpec.model_json_schema(by_alias=True)
    if "templates" in want:
        out["templates"] = core.templates.list()
    if "caption_styles" in want:
        out["caption_styles"] = core.templates.caption_styles()
    if "voices" in want:
        out["voices"] = [{k: v[k] for k in ("alias", "language", "model_id", "speed",
                                             "words_per_minute", "is_default")}
                         for v in core.profiles.list()]
    if "budget" in want:
        out["budget"] = core.budget.status()
        out["quota"] = core.voice.quota_status()
    if "settings" in want:
        out["settings"] = core.settings.get_flat()
    out["summary"] = ("FVS guide: " + ", ".join(k for k in out if k != "summary") + ".")
    return out


# ---- library and projects ---------------------------------------------------------------


class ScanArgs(Args):
    folder: str | None = Field(None, description="A folder inside the input folder; "
                                                 "default the input folder itself")
    subfolders: bool = False
    cursor: int | None = Field(None, description="next_cursor from the previous page")


def library_scan(core: Core, a: ScanArgs) -> dict[str, Any]:
    result = core.library.scan(a.folder, subfolders=a.subfolders) if a.cursor is None \
        else None
    page = core.library.list(cursor=a.cursor or 0, limit=PAGE_SIZE)
    out = {"clips": page["items"], "next_cursor": page["next_cursor"]}
    if result is not None:
        out |= {"new": [c["number"] for c in result.new], "known": result.known,
                "skipped": result.skipped, "summary": result.summary()}
    else:
        out["summary"] = f"{len(page['items'])} clip(s) from #{a.cursor} on."
    if page["next_cursor"] is not None:
        out["hint"] = "More clips: call again with cursor set to next_cursor."
    return out


class CreateArgs(Args):
    clips: list[Ref] = Field(min_length=1, description="Clip numbers, e.g. [4, 7, 9]")
    one_per_clip: bool = Field(False, description="Make one project per clip instead of "
                                                  "one project from all of them")
    title: str | None = None
    series: str | None = Field(None, description="Series name for a multi-part series")
    part: int | None = Field(None, ge=1)
    template: str | None = Field(None, description="Template name from fvs_guide")


def project_create(core: Core, a: CreateArgs) -> dict[str, Any]:
    groups = [[c] for c in a.clips] if a.one_per_clip else [a.clips]
    made = [core.projects.create(clips=g, title=a.title if len(groups) == 1 else None,
                                 series=a.series, part=a.part, template=a.template)
            for g in groups]
    nums = ", ".join(f"#{p['number']}" for p in made)
    return {"projects": made, "summary": f"Created project(s) {nums}."}


class ProjectGetArgs(Args):
    project: Ref = _ref_field("Project")
    parts: list[Literal[PROJECT_PARTS]] | None = Field(
        None, description="Only these parts; default all")
    response_format: Format = "concise"


def _renders(core: Core, proj: dict[str, Any], detailed: bool) -> list[dict[str, Any]]:
    with core.db.connect() as conn:
        rows = conn.execute("SELECT * FROM renders WHERE project_id = ? ORDER BY created_at "
                            "DESC", (proj["id"],)).fetchall()
    out = []
    for r in rows:
        qa = json.loads(r["qa_report"]) if r["qa_report"] else None
        item = {"id": r["id"], "current": r["id"] == proj["render_id"], "path": r["path"],
                "duration_s": r["duration_s"], "spec_hash": r["spec_hash"],
                "created_at": r["created_at"], "contact_sheet": r["contact_sheet_path"],
                "qa": qa if detailed or qa is None else
                {"status": qa["status"], "summary": qa["summary"],
                 "problems": [c for c in qa["checks"] if c["status"] != "pass"]}}
        out.append(item)
    return out


def project_get(core: Core, a: ProjectGetArgs) -> dict[str, Any]:
    proj = core.projects.get_row(a.project)
    want = set(a.parts or PROJECT_PARTS)
    detailed = a.response_format == "detailed"
    out: dict[str, Any] = {"project": project_public(proj),
                           "state_label": STATE_LABELS[proj["state"]],
                           "clips": [{"number": c["number"], "file_name": c["file_name"],
                                      "duration_s": c["duration_s"]}
                                     for c in core.projects.clips(proj["id"])]}
    if "script" in want and proj["script_v"]:
        out["script"] = core.scripts.current(proj)
    timings = None
    if want & {"timings", "timeline"} and proj["voiceover_id"]:
        try:
            timings = core.voice.timings(proj)
        except FvsError as exc:
            out["timings"] = {"error": exc.to_dict()}
    if "timings" in want and timings is not None:
        out["timings"] = timings if detailed else {
            "duration_s": timings.get("duration_s"),
            "sentences": [{"i": s["i"], "start": s["start"], "end": s["end"],
                           "text": s["caption"]} for s in timings["sentences"]]}
    current = core.specs.current(proj)
    if "spec" in want and current is not None:
        out["spec"] = current
    if "timeline" in want and current is not None and timings is not None:
        try:
            out["timeline"] = core.specs.check(proj, current["spec"])[1]
        except FvsError as exc:
            out["timeline"] = {"error": exc.to_dict()}
    if "renders" in want:
        out["renders"] = _renders(core, proj, detailed)
    if "reviews" in want:
        with core.db.connect() as conn:
            out["reviews"] = [dict(r) for r in conn.execute(
                "SELECT at, decision, note, by, qa_override, render_id FROM reviews "
                "WHERE project_id = ? ORDER BY id DESC LIMIT 20", (proj["id"],))]
    if "history" in want:
        with core.db.connect() as conn:
            out["history"] = [dict(r) for r in conn.execute(
                "SELECT at, source, tool, summary FROM activity WHERE project_id = ? "
                "ORDER BY id DESC LIMIT ?", (proj["id"], 50 if detailed else 10))]
    flags = (" (failed)" if proj["failed"] else "") + \
        (f", stale: {', '.join(json.loads(proj['stale_flags']))}"
         if json.loads(proj["stale_flags"]) else "")
    out["summary"] = (f"#{proj['number']} {proj['title']}: {STATE_LABELS[proj['state']]}"
                      f"{flags}.")
    return out


# ---- seeing and hearing (image tools) ---------------------------------------------------


class InspectArgs(Args):
    clip: Ref = _ref_field("Clip")
    extras: list[Literal[EXTRAS]] | None = Field(
        None, description="Which analyses; default all (scenes, transcript, loudness, "
                          "black_frozen)")
    response_format: Format = "concise"


def clip_inspect(core: Core, a: InspectArgs) -> Seen:
    result = core.analysis.inspect(a.clip, a.extras)
    sheet = result.get("contact_sheet")
    images = [Path(sheet["path"]).read_bytes()] if sheet and Path(sheet["path"]).exists() \
        else []
    if a.response_format == "concise" and isinstance(result.get("transcript"), dict):
        t = result["transcript"]
        result["transcript"] = {k: t[k] for k in ("available", "has_speech", "language",
                                                  "text", "fix") if k in t}
    if result.get("status") == "analysing":
        result["summary"] = (f"Clip #{result['clip']['number']} is being analysed (job "
                             f"{result['job_id']}); wait with fvs_job_get.")
    else:
        result["summary"] = describe(result["clip"], result)
    return Seen(images, result)


class FramesArgs(Args):
    source: Literal["clip", "render", "spec"] = Field(
        description="clip: a source clip; render: the project's current render; spec: the "
                    "current Edit Spec (or the draft in spec) drawn without rendering")
    clip: Ref | None = Field(None, description="For source clip")
    project: Ref | None = Field(None, description="For source render or spec")
    times: list[float] | None = Field(None, max_length=24,
                                      description="Exact times in seconds")
    start: float | None = Field(None, ge=0)
    end: float | None = Field(None, ge=0)
    count: int | None = Field(None, ge=1, le=24, description="Frames spread over start-end")
    safe_zone: bool = Field(False, description="Draw the TikTok safe zone (render, spec)")
    captions: bool = Field(True, description="Draw captions on spec stills")
    spec: dict[str, Any] | None = Field(None, description="A draft Edit Spec to draw "
                                                          "instead of the saved one")


def frames(core: Core, a: FramesArgs) -> Seen:
    return core.perception.frames(source=a.source, clip=a.clip, project=a.project,
                                  times=a.times, start=a.start, end=a.end, count=a.count,
                                  safe_zone=a.safe_zone, captions=a.captions, spec=a.spec)


class StoryboardArgs(Args):
    project: Ref = _ref_field("Project")
    source: Literal["spec", "render"] = "spec"
    version: int | None = Field(None, description="A saved Edit Spec version")
    spec: dict[str, Any] | None = Field(None, description="A draft Edit Spec, e.g. the one "
                                                          "fvs_edit_plan proposed")
    page: int = Field(1, ge=1)


def storyboard(core: Core, a: StoryboardArgs) -> Seen:
    return core.perception.storyboard(a.project, spec=a.spec, version=a.version,
                                      source=a.source, page=a.page)


class ListenArgs(Args):
    target: Literal["voiceover", "render", "clip"]
    project: Ref | None = Field(None, description="For voiceover or render")
    clip: Ref | None = Field(None, description="For clip")


def listen(core: Core, a: ListenArgs) -> Seen:
    return core.perception.listen(target=a.target, project=a.project, clip=a.clip)


# ---- script, voice, edit, render ---------------------------------------------------------


class ScriptArgs(Args):
    project: Ref = _ref_field("Project")
    voiceover: str = Field(min_length=1, description="One sentence per line, with markup "
                                                     "(see fvs_guide markup)")
    title: str | None = None
    hook: str | None = Field(None, description="On-screen hook, up to 8 words")
    post_caption: str | None = None
    hashtags: list[str] | None = None
    language: str | None = Field(None, description="e.g. en; default from settings")
    notes: str | None = None
    voice: str | None = Field(None, description="Voice alias from fvs_guide")


def script_save(core: Core, a: ScriptArgs) -> dict[str, Any]:
    return core.scripts.save(a.project, voiceover=a.voiceover, title=a.title, hook=a.hook,
                             post_caption=a.post_caption, hashtags=a.hashtags,
                             language=a.language, notes=a.notes, voice=a.voice)


class VoiceArgs(Args):
    project: Ref = _ref_field("Project")
    voice: str | None = Field(None, description="Voice alias from fvs_guide; default from "
                                                "settings")
    dry_run: bool = False
    allow_over_budget: bool = Field(False, description="Only after the creator agreed in "
                                                       "chat")


def voiceover_generate(core: Core, a: VoiceArgs) -> dict[str, Any]:
    return core.voice.generate(a.project, voice=a.voice, dry_run=a.dry_run,
                               allow_over_budget=a.allow_over_budget)


class PlanArgs(Args):
    project: Ref = _ref_field("Project")
    template: str | None = Field(None, description="Template name from fvs_guide")
    changes: dict[str, Any] | None = Field(None, description="Edit Spec fields to change "
                                                             "on top of the template")


def edit_plan(core: Core, a: PlanArgs) -> dict[str, Any]:
    return core.planner.propose(a.project, template=a.template, changes=a.changes)


class EditSaveArgs(Args):
    project: Ref = _ref_field("Project")
    spec: dict[str, Any] | None = Field(None, description="A whole Edit Spec")
    patch: dict[str, Any] | None = Field(None, description="A JSON merge patch on the "
                                                           "current spec")
    dry_run: bool = Field(False, description="Check only; problems come back as data")


def edit_save(core: Core, a: EditSaveArgs) -> dict[str, Any]:
    return core.specs.save(a.project, spec=a.spec, patch=a.patch, dry_run=a.dry_run)


class RenderArgs(Args):
    projects: list[Ref] = Field(min_length=1, description="Project numbers")


def render(core: Core, a: RenderArgs) -> dict[str, Any]:
    return core.renders.start(a.projects)


# ---- jobs -------------------------------------------------------------------------------


class JobGetArgs(Args):
    jobs: list[str] | Literal["active"] = Field(
        "active", description="Job IDs, or \"active\" for every queued or running job")
    wait_s: float = Field(10.0, ge=0, le=JOB_WAIT_MAX_S,
                          description="Wait up to this long for any of them to change")


def _with_eta(job: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime

    p = job.get("progress") or 0
    if job["state"] == "running" and job.get("started_at") and 0.02 < p < 1:
        elapsed = (datetime.now().astimezone()
                   - datetime.fromisoformat(job["started_at"])).total_seconds()
        job["seconds_left"] = round(elapsed * (1 - p) / p)
    return job


def _job_ids(core: Core, jobs: list[str] | str) -> list[str]:
    return [j["id"] for j in core.jobs.active()] if jobs == "active" else list(jobs)


def _job_summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No active jobs."
    parts = []
    for j in items:
        bit = f"{j['kind']} {j['id'][:8]} {j['state']}"
        if j["state"] == "running":
            bit += f" {round(100 * (j['progress'] or 0))}%"
        if j["error"]:
            bit += f" ({j['error']['code']})"
        parts.append(bit)
    return "Jobs: " + "; ".join(parts) + "."


async def job_get(core: Core, a: JobGetArgs, progress: Progress | None) -> dict[str, Any]:
    ids = await asyncio.to_thread(_job_ids, core, a.jobs)
    snapshot = [core.jobs.get(i) for i in ids]
    if a.wait_s > 0 and any(j["state"] in ACTIVE_STATES for j in snapshot):
        wanted = set(ids)
        deadline = time.monotonic() + a.wait_s

        def mine(e: dict[str, Any]) -> bool:
            return (e["type"] == "job" and e["job"]["id"] in wanted) or \
                (e["type"] == "job_progress" and e["job_id"] in wanted)

        async with core.bus.subscribe(mine) as queue:
            while (left := deadline - time.monotonic()) > 0:
                try:
                    event = await asyncio.wait_for(queue.get(), left)
                except TimeoutError:
                    break
                if event["type"] == "job":
                    break  # a state change: report it now
                if progress is not None:
                    await progress(event["progress"], event.get("note"))
        snapshot = [core.jobs.get(i) for i in ids]
    items = [_with_eta(j) for j in snapshot]
    return {"jobs": items, "summary": _job_summary(items)}


class JobCancelArgs(Args):
    job_id: str


def job_cancel(core: Core, a: JobCancelArgs) -> dict[str, Any]:
    job = core.jobs.cancel(a.job_id)
    return {"job": job, "summary": f"Job {a.job_id[:8]} is {job['state']}."}


# ---- settings ---------------------------------------------------------------------------


class SettingsArgs(Args):
    changes: dict[str, Any] | None = Field(
        None, description='Settings to change as dotted keys, e.g. {"video.max_seconds": '
                          '60, "voice.daily_char_budget": 20000}; omit to read')


def settings(core: Core, a: SettingsArgs) -> dict[str, Any]:
    if a.changes:
        core.settings.update(a.changes, source="claude")
        summary = "Changed " + ", ".join(sorted(a.changes)) + "."
    else:
        summary = "Read the settings."
    return {"settings": core.settings.get_flat(), "summary": summary}


# ---- the registry -----------------------------------------------------------------------


def build_registry() -> Registry:
    r = Registry()
    r.add(Tool("fvs_guide", "Load the guide", "Start here, once per conversation. Returns "
               "the workflow, the script markup, the Edit Spec JSON schema, templates, "
               "caption styles, voices, today's character budget, the ElevenLabs quota "
               "left and the settings. Free.", GuideArgs, guide, read_only=True,
               idempotent=True))
    r.add(Tool("fvs_library_scan", "Scan for clips", "Find new clips in the creator's "
               "input folder and list every clip with its number, length, resolution and "
               "sound. Call before creating projects. Lists 20 clips per page; pass "
               "next_cursor to read on. Free.", ScanArgs, library_scan, idempotent=True,
               example='{"subfolders": false}'))
    r.add(Tool("fvs_project_create", "Create projects", "Create a project (one video) from "
               "one or more clips; one_per_clip makes one project per clip. Use series and "
               "part for a multi-part series. Returns project numbers. Free.", CreateArgs,
               project_create, example='{"clips": [4, 7, 9], "title": "Volcano facts"}'))
    r.add(Tool("fvs_project_get", "Read a project", "Everything about one project in one "
               "call: state, script, sentence and word timings, Edit Spec, resolved "
               "timeline, renders with QA, review notes and history. Use before revising "
               "a video. Free.", ProjectGetArgs, project_get, read_only=True,
               idempotent=True, example='{"project": 3}'))
    r.add(Tool("fvs_clip_inspect", "Inspect a clip", "Analyse a clip: details, scenes, "
               "speech in it, loudness, black or frozen parts, and a contact sheet image. "
               "Cached after the first time. If it returns a job_id, wait with fvs_job_get "
               "and call again. Free.", InspectArgs, clip_inspect, read_only=True,
               idempotent=True, images=True, example='{"clip": 7}'))
    r.add(Tool("fvs_frames", "Look at frames", "Your eyes: frames at exact times (or "
               "count frames over start-end) from a clip, a render, or the Edit Spec "
               "(source spec draws the edit without rendering, so use it before "
               "rendering). Up to 6 frames come back as separate images, more as one "
               "sheet. safe_zone draws the area captions must stay in. Free.", FramesArgs,
               frames, read_only=True, idempotent=True, images=True,
               example='{"source": "render", "project": 3, "times": [0.5, 4.2], '
                       '"safe_zone": true}'))
    r.add(Tool("fvs_storyboard", "See the storyboard", "The whole edit as one picture: one "
               "tile per segment (first frame, time, clip) plus a table with the source "
               "time and the words spoken over it. Pass a draft spec to check a plan before "
               "saving it. Free.", StoryboardArgs, storyboard, read_only=True,
               idempotent=True, images=True, example='{"project": 3}'))
    r.add(Tool("fvs_listen", "Listen", "Your ears: transcript of the voiceover, render or "
               "clip with the words that differ from the script, a loudness chart per "
               "track (voice, original sound, music, mix), silences over 0.7 s, clipping, "
               "loudness and true peak. A long transcript returns a job_id first. Free.",
               ListenArgs, listen, read_only=True, idempotent=True, images=True,
               example='{"project": 3, "target": "render"}'))
    r.add(Tool("fvs_script_save", "Save a script", "Save a new script version: the "
               "voiceover (one sentence per line, markup in fvs_guide), hook, post caption "
               "and hashtags. Returns numbered sentences, the estimated length and whether "
               "it fits. SCRIPT_TOO_LONG says how many words to cut. Free.", ScriptArgs,
               script_save, idempotent=True,
               example='{"project": 3, "voiceover": "This volcano makes its own '
                       'lightning.\\nThen the whole sky lights up.", "hook": "Volcanoes '
                       'make lightning"}'))
    r.add(Tool("fvs_voiceover_generate", "Make the voiceover", "Make the voiceover for one "
               "project from its latest script, sentence by sentence, with ElevenLabs. "
               "COSTS MONEY: ElevenLabs charges per character, but unchanged sentences are "
               "reused free. Use dry_run first for batches. If it fails with "
               "BUDGET_EXCEEDED, ask the creator in chat, then call again with "
               "allow_over_budget. Returns every sentence's start and end time; use them "
               "as cues in the Edit Spec. If fit is 'too_long', shorten the script with "
               "fvs_script_save. A long voiceover returns a job_id.", VoiceArgs,
               voiceover_generate, idempotent=True, costs_money=True,
               example='{"project": 3, "dry_run": true}'))
    r.add(Tool("fvs_edit_plan", "Propose an edit", "Propose an Edit Spec (not saved): which "
               "shot plays under each sentence, from the template and the clip analysis, "
               "with notes on problems such as a clip too short for its sentence. Check it "
               "with fvs_storyboard (pass it as spec), adjust, then fvs_edit_save. Free.",
               PlanArgs, edit_plan, read_only=True, idempotent=True,
               example='{"project": 3, "template": "storytime_dark"}'))
    r.add(Tool("fvs_edit_save", "Save the edit", "Validate and save an Edit Spec (whole, or "
               "a merge patch on the current one). Returns the version, its hash, the "
               "resolved timeline and every problem with its path and fix. dry_run checks "
               "without saving. Free.", EditSaveArgs, edit_save, idempotent=True,
               example='{"project": 3, "patch": {"captions": {"position": "center"}}}'))
    r.add(Tool("fvs_render", "Render", "Render projects from their saved Edit Spec. Returns "
               "a job_id per project at once (or reuses an identical earlier render); wait "
               "with fvs_job_get. Each render ends with 8 quality checks. Free.",
               RenderArgs, render, idempotent=True, example='{"projects": [3]}'))
    r.add(Tool("fvs_job_get", "Check jobs", "Check jobs (or all active ones), waiting up to "
               "15 s for any of them to change. Returns state, progress, seconds left, the "
               "result, or the error with its fix. Call again while they run. Free.",
               JobGetArgs, job_get, read_only=True, idempotent=True,
               example='{"jobs": ["<job_id>"], "wait_s": 15}'))
    r.add(Tool("fvs_job_cancel", "Cancel a job", "Stop a queued or running job, such as a "
               "render started by mistake. Finished work is kept and a cancelled render "
               "leaves no partial file. Free.", JobCancelArgs, job_cancel, idempotent=True,
               example='{"job_id": "<job_id>"}'))
    r.add(Tool("fvs_settings", "Settings", "Read or change settings: video length, daily "
               "character budget, export folder, default template and voice, renders at "
               "once, safe zone. Changes are checked like in the web app and logged. The "
               "ElevenLabs key is never readable here. Free.", SettingsArgs, settings,
               idempotent=True, example='{"changes": {"video.max_seconds": 60}}'))
    return r
