"""Renders (RND-1–5; DS 5.5, 10.2, 10.3).

- One render per spec hash. Starting a render for a spec that was already rendered
  reuses the file (RND-5) instead of running FFmpeg again.
- A render is a background job with live progress and can be cancelled (RND-3).
- FFmpeg runs as an argument list from the project folder, so caption and font paths are
  short relative paths (no Windows drive-colon escaping).
- Loudness takes two passes: an audio-only pass measures, the render applies it (AUD-3).
- Output goes to a temp file and is renamed only when complete (NFR-R1).
- The exact commands, the end of FFmpeg's output and the time taken are kept in the
  ``renders`` row and the job log (RND-4). Failures are classified with a fix.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from fvs.activity import ActivityLog, Source
from fvs.analyze.probe import probe
from fvs.atomic import atomic_path, write_text_atomic
from fvs.compile.compiler import Inputs, compile_render, parse_loudnorm
from fvs.errors import ErrorCode, FvsError
from fvs.jobs import JobCancelled, JobContext, JobQueue, StepContext
from fvs.logs import get_logger
from fvs.media.tools import Result, run_tool
from fvs.projects import Projects, next_state
from fvs.qa.runner import run_qa
from fvs.script.service import ScriptService
from fvs.see.transcripts import Transcripts
from fvs.settings import SettingsService
from fvs.spec.service import SpecService
from fvs.spec.templates import Templates
from fvs.store import Database
from fvs.timeutil import now_iso

FONT_EXTS = (".ttf", ".otf")
log = get_logger("fvs.render")


def font_family(fonts_dir: Path, style: dict[str, Any]) -> tuple[str, str | None]:
    """The style's font if a file for it is in the fonts folder, else a system fallback."""
    family = style["font"]
    key = family.lower().replace(" ", "")
    if fonts_dir.is_dir() and any(p.suffix.lower() in FONT_EXTS
                                  and p.name.lower().replace(" ", "").startswith(key)
                                  for p in fonts_dir.iterdir()):
        return family, None
    fallback = "Arial Black" if style.get("weight", 400) >= 800 else "Arial"
    return fallback, (f"The font {family} is not installed in {fonts_dir}; captions used "
                      f"{fallback} instead.")


def classify(res: Result, log_path: Path) -> FvsError:
    tail = res.stderr_tail(6)
    low = res.stderr.lower()
    if "unknown encoder" in low or "encoder not found" in low:
        return FvsError(ErrorCode.RENDER_FAILED, "This FFmpeg build has no H.264 encoder "
                        "(libx264).", "Install the full FFmpeg 7 build and run `fvs doctor`.")
    if "no space left" in low:
        return FvsError(ErrorCode.RENDER_FAILED, "The disk is full; the render stopped.",
                        "Free some space on the workspace drive and render again.")
    if "no such file" in low or "error opening input" in low:
        return FvsError(ErrorCode.RENDER_FAILED, "A clip or voice file could not be opened: "
                        f"{tail}", "Check that the clip is still in the input folder "
                        "(fvs_library_scan), then render again.")
    if "invalid data found" in low or "moov atom not found" in low:
        return FvsError(ErrorCode.RENDER_FAILED, f"FFmpeg could not read a clip: {tail}",
                        "Re-export the clip as MP4 (H.264) and scan again.")
    return FvsError(ErrorCode.RENDER_FAILED, f"FFmpeg stopped with an error: {tail}",
                    f"See the full log at {log_path}. If the spec looks right, report it.")


class RenderService:
    def __init__(self, db: Database, *, projects: Projects, scripts: ScriptService,
                 specs: SpecService, templates: Templates, settings: SettingsService,
                 jobs: JobQueue, activity: ActivityLog, transcripts: Transcripts,
                 fonts_dir: Path) -> None:
        self.db = db
        self.projects = projects
        self.scripts = scripts
        self.transcripts = transcripts
        self.specs = specs
        self.templates = templates
        self.settings = settings
        self.jobs = jobs
        self.activity = activity
        self.fonts_dir = fonts_dir
        jobs.register("render", self.run_job)

    # ---- starting --------------------------------------------------------------------

    def _existing(self, spec_hash: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM renders WHERE spec_hash = ?",
                               (spec_hash,)).fetchone()
        return dict(row) if row is not None and Path(row["path"]).exists() else None

    def start(self, projects: list[int | str], *, source: Source = "claude"
              ) -> dict[str, Any]:
        items = []
        for ref in projects:
            proj = self.projects.get_row(ref)
            current = self.specs.current(proj)
            if current is None:
                raise FvsError(ErrorCode.WRONG_STATE,
                               f"Project #{proj['number']} has no Edit Spec to render.",
                               "Save one with fvs_edit_save first.")
            existing = self._existing(current["spec_hash"])
            if existing is not None:
                items.append(self._reuse(proj, existing))
                continue
            next_state(proj, "render_started")
            job = self.jobs.submit("render", {"spec_version": current["version"],
                                              "source": source},
                                   project_id=proj["id"], reuse_done=False,
                                   idempotency_key=f"render:{current['spec_hash']}")
            with self.db.transaction() as conn:
                row = self.projects.get_row(proj["id"], conn)
                self.projects.advance(conn, row, "render_started")
            items.append({"project": proj["number"], "status": "rendering",
                          "job_id": job["id"]})
            self.activity.add(f"Started rendering #{proj['number']} (Edit Spec "
                              f"v{current['version']}).", source=source,
                              project_id=proj["id"],
                              tool="fvs_render" if source == "claude" else None)
        reused = sum(1 for i in items if i["status"] == "reused")
        return {"items": items,
                "summary": f"{len(items) - reused} render(s) started, {reused} reused."}

    def _reuse(self, proj: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
        """Point the project at an earlier render of the same spec (RND-5)."""
        with self.db.transaction() as conn:
            row = self.projects.get_row(proj["id"], conn)
            if row["render_id"] != existing["id"] and row["state"] == "planned":
                self.projects.advance(conn, row, "render_started")
                row = self.projects.get_row(proj["id"], conn)
                self.projects.advance(conn, row, "render_done",
                                      fields={"render_id": existing["id"]},
                                      stale_remove=("render",))
        return {"project": proj["number"], "status": "reused", "render_id": existing["id"],
                "path": existing["path"]}

    def render_in(self, parent: JobContext, ref: int | str, *, start: float,
                  end: float) -> dict[str, Any]:
        """Render one project inside another job's worker (a batch step)."""
        proj = self.projects.get_row(ref)
        current = self.specs.current(proj)
        if current is None:
            raise FvsError(ErrorCode.WRONG_STATE,
                           f"Project #{proj['number']} has no Edit Spec to render.",
                           "Add the plan step, or save a spec with fvs_edit_save.")
        existing = self._existing(current["spec_hash"])
        if existing is not None:
            return self._reuse(proj, existing)
        with self.db.transaction() as conn:
            self.projects.advance(conn, self.projects.get_row(proj["id"], conn),
                                  "render_started")
        return self.run_job(StepContext(parent, project_id=proj["id"],
                                        payload={"spec_version": current["version"],
                                                 "source": "claude"},
                                        start=start, end=end))

    # ---- the job ---------------------------------------------------------------------

    def inputs(self, proj: dict[str, Any], spec: Any, ctx: Any) -> tuple[Inputs, list[str]]:
        style = self.templates.caption_styles()[spec.captions.style]
        family, font_warning = font_family(self.fonts_dir, style)
        with self.db.connect() as conn:
            voice = conn.execute("SELECT audio_path FROM voiceovers WHERE id = ?",
                                 (proj["voiceover_id"],)).fetchone()
        music = ctx.assets.get(spec.audio.music.asset) if spec.audio.music else None
        fonts_rel = Path("..", "..", "assets", "fonts").as_posix()  # from the project folder
        return (Inputs(clips=ctx.clips, voice_path=Path(voice["audio_path"]),
                       timings=ctx.timings, style=style, font=family,
                       safe=self.settings.get().safe_zone, hook_text=ctx.script_hook,
                       music=music, fonts_dir=fonts_rel if self.fonts_dir.is_dir() else None),
                [font_warning] if font_warning else [])

    def run_job(self, ctx: JobContext) -> dict[str, Any]:
        started = time.monotonic()
        proj = self.projects.get_row(ctx.project_id or "")
        current = self.specs.current(proj)
        if current is None or current["version"] != ctx.payload["spec_version"]:
            raise FvsError(ErrorCode.WRONG_STATE,
                           f"The Edit Spec of #{proj['number']} changed before the render "
                           "started.", "Call fvs_render again for the current spec.")
        spec, timeline, spec_hash, sctx = self.specs.check(proj, current["spec"])
        inputs, warnings = self.inputs(proj, spec, sctx)
        folder = self.projects.folder(proj)
        ass_name = f"captions/{spec_hash}.ass"
        final = folder / "renders" / f"{spec_hash}.mp4"
        log_path = folder / "logs" / f"{ctx.job_id}.log"
        (folder / "renders").mkdir(parents=True, exist_ok=True)

        plan = compile_render(spec, timeline, inputs, ass_name=ass_name, out_name="-")
        if plan.ass is not None:
            write_text_atomic(folder / ass_name, plan.ass)
        ctx.progress(0.02, "Measuring loudness")
        measure = run_tool("ffmpeg", plan.measure_args, cwd=folder,
                           cancelled=lambda: ctx.cancelled, timeout=600)
        try:
            measured = parse_loudnorm(measure.stderr)
        except ValueError:
            self._log(log_path, [measure])
            raise classify(measure, log_path) from None

        total = plan.duration_s

        def on_line(line: str) -> None:
            if line.startswith("out_time_us=") and line[12:].isdigit():
                done = int(line[12:]) / 1e6
                ctx.progress(0.1 + 0.7 * min(1.0, done / total),
                             f"Rendering: {done:.0f} of {total:.0f} s")

        with atomic_path(final) as tmp:
            plan = compile_render(spec, timeline, inputs, ass_name=ass_name,
                                  out_name=Path("renders", tmp.name).as_posix(),
                                  measured=measured)
            plan.args[-1:-1] = ["-f", "mp4"]  # the temp name has no .mp4 extension
            res = run_tool("ffmpeg", plan.args, cwd=folder, on_line=on_line,
                           cancelled=lambda: ctx.cancelled, timeout=1800)
            self._log(log_path, [measure, res])
            if res.returncode != 0:
                raise classify(res, log_path)

        info = probe(final)
        render_seconds = round(time.monotonic() - started, 1)
        report = self._qa(final=final, folder=folder,
                          ass_name=ass_name if plan.ass is not None else None,
                          ass_text=plan.ass, spec=spec, timeline=timeline, inputs=inputs,
                          proj=proj, ctx=ctx)
        seconds = round(time.monotonic() - started, 1)
        render_id = uuid.uuid4().hex
        command_log = {"measure": subprocess.list2cmdline(measure.args),
                       "render": subprocess.list2cmdline(res.args),
                       "stderr_tail": res.stderr_tail(20), "seconds": render_seconds,
                       "log": str(log_path)}
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO renders (id, project_id, spec_hash, path, duration_s, bytes, "
                "qa_report, contact_sheet_path, command_log, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (render_id, proj["id"], spec_hash, str(final), info.duration_s,
                 final.stat().st_size, json.dumps(report), report.get("contact_sheet"),
                 json.dumps(command_log), now_iso()))
            row = self.projects.get_row(proj["id"], conn)
            self.projects.advance(conn, row, "render_done", fields={"render_id": render_id},
                                  stale_remove=("render",))
        return {"project": proj["number"], "render_id": render_id, "path": str(final),
                "duration_s": info.duration_s, "bytes": final.stat().st_size,
                "seconds": seconds, "render_seconds": render_seconds, "warnings": warnings,
                "qa": {"status": report["status"], "summary": report["summary"]},
                "summary": f"Rendered #{proj['number']}: a {info.duration_s:.1f} s video "
                           f"in {render_seconds:.0f} s. {report['summary']}"}

    def _qa(self, *, proj: dict[str, Any], ctx: JobContext, **kw: Any) -> dict[str, Any]:
        """The QA report; a check that cannot run never loses the finished render."""
        sentences = [s["spoken"] for s in self.scripts.sentences_for_voice(proj)]
        try:
            return run_qa(**kw, fonts_dir=self.fonts_dir, transcripts=self.transcripts,
                          sentences=sentences, cancelled=lambda: ctx.cancelled,
                          progress=lambda frac, note: ctx.progress(0.8 + 0.2 * frac, note))
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - reported in the QA result instead
            log.warning("qa_failed", project=proj["number"], error=str(exc))
            return {"status": "warn", "checks": [], "contact_sheet": None,
                    "summary": f"QA could not run ({exc}); check the render with fvs_frames "
                               "and fvs_listen."}

    @staticmethod
    def _log(path: Path, results: list[Result]) -> None:
        text = "\n\n".join(f"$ {subprocess.list2cmdline(r.args)}\n[exit {r.returncode}]\n"
                           f"{r.stderr}" for r in results)
        write_text_atomic(path, text)


def install_fonts(bundled: Path, fonts_dir: Path) -> None:
    """Copy the fonts that ship with FVS into the workspace fonts folder (NFR-X1)."""
    fonts_dir.mkdir(parents=True, exist_ok=True)
    if not bundled.is_dir():
        return
    for p in bundled.iterdir():
        if p.suffix.lower() in FONT_EXTS and not (fonts_dir / p.name).exists():
            shutil.copy2(p, fonts_dir / p.name)
