"""The perception service: Claude's eyes and ears (SEE-2–5, SEE-7; DS 8.3).

- ``frames``: stills at exact times or spread over a range, from a clip, a render, or
  the current or a draft Edit Spec (without a full render). Up to 6 come back as single
  images of at most 512 px; more become one sheet of at most 1024 px (MCP-4).
- ``storyboard``: one tile per segment with a table, 12 tiles per page.
- ``listen``: transcript and differences from the script, loudness per track as a
  chart, silences, clipping, loudness and true peak. Transcribing that would take more
  than 15 s runs as a job (MCP-3); the measurements always come back at once.
"""

from __future__ import annotations

import io
import json
import math
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from fvs.analyze.sheet import fmt_time, grab_frame
from fvs.atomic import write_text_atomic
from fvs.compile.compiler import AUDIO_FMT, FPS, audio_stems, caption_ass, input_args
from fvs.compile.graph import Graph, f
from fvs.errors import ErrorCode, FvsError
from fvs.jobs import JobContext, JobQueue
from fvs.library import Library
from fvs.media.tools import run_tool
from fvs.projects import Projects
from fvs.render.service import RenderService
from fvs.script.service import ScriptService
from fvs.see import listen as ears
from fvs.see.images import MAX_IMAGE, draw_safe_zone, fit, grid
from fvs.see.stills import STILL_H, STILL_W, still_args
from fvs.see.transcripts import Transcripts, heard_words
from fvs.settings import SettingsService
from fvs.spec.service import SpecService
from fvs.store import Database

MAX_FRAMES = 24
SINGLE_FRAMES = 6  # up to this many come back as separate images
INLINE_LIMIT_S = 15.0
TILE_W, TILE_H = 162, 288
TILES_PER_PAGE = 12
SOURCES = ("clip", "render", "spec")
TARGETS = ("voiceover", "render", "clip")


@dataclass
class Seen:
    """What an image tool returns: JPEG images plus their data (MCP-2)."""

    images: list[bytes]
    data: dict[str, Any]


def jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def _bad(message: str, fix: str, example: str | None = None) -> FvsError:
    return FvsError(ErrorCode.SPEC_INVALID, message, fix, example=example)


def pick_times(duration_s: float, times: list[float] | None, start: float | None,
               end: float | None, count: int | None) -> list[float]:
    """Exact times, or ``count`` times spread evenly over ``start``–``end``."""
    last = max(0.0, duration_s - 1 / FPS)
    if times:
        if len(times) > MAX_FRAMES:
            raise _bad(f"{len(times)} times asked for; the limit is {MAX_FRAMES}.",
                       "Ask for fewer times, or use start, end and count.")
        late = [t for t in times if t < 0 or t > duration_s]
        if late:
            raise _bad(f"Time {late[0]:g} s is outside 0–{duration_s:.2f} s.",
                       "Use times within the video's length.")
        return [round(min(t, last), 3) for t in times]
    a = 0.0 if start is None else start
    b = duration_s if end is None else end
    n = SINGLE_FRAMES if count is None else count
    if not 0 <= a < b <= duration_s + 1e-6:
        raise _bad(f"The range {a:g}–{b:g} s does not fit 0–{duration_s:.2f} s.",
                   "Use 0 ≤ start < end ≤ the video's length.")
    if not 1 <= n <= MAX_FRAMES:
        raise _bad(f"count {n} is outside 1–{MAX_FRAMES}.", f"Use a count of 1–{MAX_FRAMES}.")
    step = (b - a) / n
    return [round(min(a + (k + 0.5) * step, last), 3) for k in range(n)]


def _frame_size(width: int, height: int, box: int) -> tuple[int, int]:
    scale = box / max(width, height)
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


class Perception:
    def __init__(self, db: Database, *, library: Library, projects: Projects,
                 scripts: ScriptService, specs: SpecService, renders: RenderService,
                 settings: SettingsService, transcripts: Transcripts, jobs: JobQueue,
                 workspace: Path) -> None:
        self.db = db
        self.library = library
        self.projects = projects
        self.scripts = scripts
        self.specs = specs
        self.renders = renders
        self.settings = settings
        self.transcripts = transcripts
        self.jobs = jobs
        self.workspace = workspace
        jobs.register("listen", self.run_listen_job)

    # ---- shared lookups --------------------------------------------------------------

    def _project(self, project: int | str | None) -> dict[str, Any]:
        if project is None:
            raise _bad("This needs a project.", "Pass project (its number).",
                       example='{"project": 3}')
        return self.projects.get_row(project)

    def render_row(self, proj: dict[str, Any]) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM renders WHERE id = ?",
                               (proj["render_id"],)).fetchone() if proj["render_id"] else None
        if row is None or not Path(row["path"]).exists():
            raise FvsError(ErrorCode.WRONG_STATE,
                           f"Project #{proj['number']} has no render yet.",
                           f"Render it with fvs_render for #{proj['number']}.")
        return dict(row)

    def spec_data(self, proj: dict[str, Any], *, spec: dict[str, Any] | None = None,
                  version: int | None = None, spec_hash: str | None = None
                  ) -> dict[str, Any]:
        """A draft spec as given, a saved version, the version with a hash, or current."""
        if spec is not None:
            return spec
        if version is not None or spec_hash is not None:
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT spec_json FROM edit_specs WHERE project_id = ? AND "
                    + ("version = ?" if version is not None else "spec_hash = ?")
                    + " ORDER BY version DESC",
                    (proj["id"], version if version is not None else spec_hash)).fetchone()
            if row is None:
                raise FvsError(ErrorCode.NOT_FOUND,
                               f"Project #{proj['number']} has no Edit Spec version "
                               f"{version if version is not None else spec_hash}.",
                               "Call fvs_project_get to see its spec versions.")
            return json.loads(row["spec_json"])
        current = self.specs.current(proj)
        if current is None:
            raise FvsError(ErrorCode.WRONG_STATE,
                           f"Project #{proj['number']} has no Edit Spec yet.",
                           "Propose one with fvs_edit_plan and save it with fvs_edit_save, "
                           "or pass a draft spec.")
        return current["spec"]

    def _stills(self, proj: dict[str, Any], data: dict[str, Any], times: list[float], *,
                captions: bool, cancelled: Callable[[], bool] | None
                ) -> tuple[list[Image.Image], dict[str, Any]]:
        spec, timeline, spec_hash, ctx = self.specs.check(proj, data)
        inputs, _ = self.renders.inputs(proj, spec, ctx)
        folder = self.projects.folder(proj)
        ass_name = None
        if captions:
            text = caption_ass(spec, inputs, timeline["duration_s"])
            if text is not None:
                ass_name = f"captions/{spec_hash}.ass"
                if not (folder / ass_name).exists():
                    write_text_atomic(folder / ass_name, text)

        def one(t: float) -> Image.Image:
            res = run_tool("ffmpeg", still_args(spec, timeline, inputs, t, ass_name=ass_name),
                           cwd=folder, timeout=60, cancelled=cancelled)
            if res.returncode != 0 or not res.stdout:
                raise FvsError(ErrorCode.RENDER_FAILED,
                               f"Could not draw the frame at {t:g} s: {res.stderr_tail(3)}",
                               "Check the clip with fvs_clip_inspect, then try again.")
            return Image.open(io.BytesIO(res.stdout)).convert("RGB")

        with ThreadPoolExecutor(max_workers=SINGLE_FRAMES) as pool:
            images = list(pool.map(one, times))
        return images, timeline

    # ---- frames (SEE-2, SEE-3) -------------------------------------------------------

    def frames(self, *, source: str, clip: int | str | None = None,
               project: int | str | None = None, times: list[float] | None = None,
               start: float | None = None, end: float | None = None,
               count: int | None = None, safe_zone: bool = False, captions: bool = True,
               spec: dict[str, Any] | None = None,
               cancelled: Callable[[], bool] | None = None) -> Seen:
        if source not in SOURCES:
            raise _bad(f"Unknown source {source!r}.", f"Use one of: {', '.join(SOURCES)}.",
                       example='{"source": "render", "project": 3, "times": [0.5, 2]}')
        info: dict[str, Any] = {"source": source}
        notes: list[str] = []
        if source == "clip":
            if clip is None:
                raise _bad("source \"clip\" needs a clip.", "Pass clip (its number).",
                           example='{"source": "clip", "clip": 7, "times": [1.5]}')
            row = self.library.get_row(clip)
            ts = pick_times(row["duration_s"] or 0.0, times, start, end, count)
            box = MAX_IMAGE if len(ts) <= SINGLE_FRAMES else 320
            size = _frame_size(row["width"] or 16, row["height"] or 9, box)
            with ThreadPoolExecutor(max_workers=SINGLE_FRAMES) as pool:
                got = list(pool.map(lambda t: grab_frame(Path(row["path"]), t, size,
                                                         cancelled), ts))
            images = [g or Image.new("RGB", size, (60, 0, 0)) for g in got]
            info["clip"] = row["number"]
            if safe_zone:
                notes.append("The safe zone is drawn only on 9:16 output (render or spec).")
        else:
            proj = self._project(project)
            info["project"] = proj["number"]
            if source == "render":
                render = self.render_row(proj)
                ts = pick_times(render["duration_s"], times, start, end, count)
                with ThreadPoolExecutor(max_workers=SINGLE_FRAMES) as pool:
                    images = [g or Image.new("RGB", (STILL_W, STILL_H), (60, 0, 0))
                              for g in pool.map(lambda t: grab_frame(
                                  Path(render["path"]), t, (STILL_W, STILL_H), cancelled), ts)]
                info["render_id"] = render["id"]
            else:
                data = self.spec_data(proj, spec=spec)
                _, timeline, _, _ = self.specs.check(proj, data)
                ts = pick_times(timeline["duration_s"], times, start, end, count)
                images, _ = self._stills(proj, data, ts, captions=captions,
                                         cancelled=cancelled)
                info["draft"] = spec is not None
            if safe_zone:
                safe = self.settings.get().safe_zone
                images = [draw_safe_zone(i, safe) for i in images]
        labels = [fmt_time(t) for t in ts]
        if len(images) <= SINGLE_FRAMES:
            out = [jpeg(fit(i, MAX_IMAGE)) for i in images]
        else:
            portrait = images[0].height > images[0].width
            out = [jpeg(grid(images, labels, cols=6 if portrait else 4))]
        what = (f"clip #{info['clip']}" if source == "clip" else
                f"the {'draft ' if info.get('draft') else ''}{source} of #{info['project']}")
        shape = f"{len(ts)} frame(s)" if len(out) > 1 or len(ts) == 1 else \
            f"{len(ts)} frames on one sheet"
        return Seen(out, {**info, "frames": [{"t": t, "label": lab}
                                              for t, lab in zip(ts, labels, strict=True)],
                          "notes": notes, "summary": f"{shape} from {what}."})

    # ---- storyboard (SEE-4) ----------------------------------------------------------

    def storyboard(self, project: int | str, *, spec: dict[str, Any] | None = None,
                   version: int | None = None, source: str = "spec", page: int = 1,
                   cancelled: Callable[[], bool] | None = None) -> Seen:
        proj = self._project(project)
        if source == "render":
            render = self.render_row(proj)
            data = self.spec_data(proj, spec_hash=render["spec_hash"])
        elif source == "spec":
            data = self.spec_data(proj, spec=spec, version=version)
        else:
            raise _bad(f"Unknown source {source!r}.", "Use \"spec\" or \"render\".")
        _, timeline, _, _ = self.specs.check(proj, data)
        segs = timeline["segments"]
        pages = max(1, math.ceil(len(segs) / TILES_PER_PAGE))
        if not 1 <= page <= pages:
            raise _bad(f"Page {page} does not exist; there are {pages}.",
                       f"Use a page from 1 to {pages}.")
        shown = segs[(page - 1) * TILES_PER_PAGE: page * TILES_PER_PAGE]
        times = [round(s["out_start"] + 0.5 / FPS, 4) for s in shown]
        if source == "render":
            with ThreadPoolExecutor(max_workers=SINGLE_FRAMES) as pool:
                tiles = [g or Image.new("RGB", (TILE_W, TILE_H), (60, 0, 0))
                         for g in pool.map(lambda t: grab_frame(
                             Path(render["path"]), t, (TILE_W, TILE_H), cancelled), times)]
        else:
            stills, _ = self._stills(proj, data, times, captions=False, cancelled=cancelled)
            tiles = [s.resize((TILE_W, TILE_H), Image.Resampling.LANCZOS) for s in stills]
        labels = [f"{s['index'] + 1} {fmt_time(s['out_start'])} #{s['clip']}" for s in shown]
        rows = [{"segment": s["index"] + 1, "start": s["out_start"], "end": s["out_end"],
                 "clip": s["clip"], "source": f"{s['src_in']:.2f}–{s['src_out']:.2f} s",
                 "speed": s["speed"], "motion": s["motion"],
                 "fit": "loop" if s["loop"] else f"hold {s['hold_s']:.1f} s"
                 if s["hold_s"] else None,
                 "transition": s["transition_in"]["type"],
                 "words": " ".join(s["text"].split()[:8])} for s in shown]
        sheet = grid(tiles, labels, cols=min(6, len(tiles)))
        return Seen([jpeg(sheet)], {
            "project": proj["number"], "source": source, "page": page, "pages": pages,
            "duration_s": timeline["duration_s"], "segments": rows,
            "summary": f"Storyboard of #{proj['number']}: {len(segs)} segment(s), "
                       f"{timeline['duration_s']:.1f} s" + (f", page {page} of {pages}"
                                                           if pages > 1 else "") + "."})

    # ---- listen (SEE-5) --------------------------------------------------------------

    def _target(self, target: str, project: int | str | None, clip: int | str | None
                ) -> dict[str, Any]:
        """The media to hear, how to build its tracks, and the script to compare with."""
        if target not in TARGETS:
            raise _bad(f"Unknown target {target!r}.", f"Use one of: {', '.join(TARGETS)}.",
                       example='{"project": 3, "target": "render"}')
        g = Graph()
        if target == "clip":
            if clip is None:
                raise _bad("target \"clip\" needs a clip.", "Pass clip (its number).")
            row = self.library.get_row(clip)
            if not row["has_audio"]:
                raise FvsError(ErrorCode.CLIP_UNREADABLE,
                               f"Clip #{row['number']} has no sound to listen to.",
                               "Listen to the voiceover or the render instead.")
            return {"media": Path(row["path"]), "duration_s": row["duration_s"],
                    "inputs": ["-vn", "-i", row["path"]], "graph": g,
                    "stems": {"clip": g.one(["0:a"], [AUDIO_FMT], "a")}, "main": "clip",
                    "sentences": None, "what": f"clip #{row['number']}"}
        proj = self._project(project)
        sentences = [s["spoken"] for s in self.scripts.sentences_for_voice(proj)]
        if target == "voiceover":
            if not proj["voiceover_id"]:
                raise FvsError(ErrorCode.WRONG_STATE,
                               f"Project #{proj['number']} has no voiceover yet.",
                               f"Make it with fvs_voiceover_generate for #{proj['number']}.")
            with self.db.connect() as conn:
                vo = conn.execute("SELECT audio_path, duration_s FROM voiceovers WHERE id = ?",
                                  (proj["voiceover_id"],)).fetchone()
            return {"media": Path(vo["audio_path"]), "duration_s": vo["duration_s"],
                    "inputs": ["-i", vo["audio_path"]], "graph": g,
                    "stems": {"voice": g.one(["0:a"], [AUDIO_FMT], "a")}, "main": "voice",
                    "sentences": sentences, "what": f"the voiceover of #{proj['number']}"}
        render = self.render_row(proj)
        spec, timeline, _, ctx = self.specs.check(
            proj, self.spec_data(proj, spec_hash=render["spec_hash"]))
        inputs, _ = self.renders.inputs(proj, spec, ctx)
        args, v_idx, m_idx = input_args(timeline, spec, inputs, audio_only=True)
        stems: dict[str, str] = {}
        parts: list[str] = []
        for name, label in audio_stems(g, spec, timeline, inputs, v_idx, m_idx).items():
            stems[name], b = g.chain([label], ["asplit"], 2, "sp")
            parts.append(b)
        stems["premix"] = parts[0] if len(parts) == 1 else g.one(
            parts, [f("amix", inputs=len(parts), normalize=0, duration="first")], "pm")
        args += ["-vn", "-i", render["path"]]
        stems["mix"] = g.one([f"{(m_idx if m_idx is not None else v_idx) + 1}:a"],
                             [AUDIO_FMT], "r")
        return {"media": Path(render["path"]), "duration_s": render["duration_s"],
                "inputs": args, "graph": g, "stems": stems, "main": "mix",
                "sentences": sentences, "what": f"the render of #{proj['number']}"}

    def listen(self, *, target: str, project: int | str | None = None,
               clip: int | str | None = None,
               cancelled: Callable[[], bool] | None = None) -> Seen:
        tg = self._target(target, project, clip)
        media, duration = tg["media"], tg["duration_s"]
        with tempfile.TemporaryDirectory(dir=self.workspace / "cache") as tmp:
            measured = ears.measure(tg["inputs"], tg["graph"], tg["stems"], tg["main"],
                                    Path(tmp), duration, cancelled)
        tracks = measured["tracks"]
        premix = tracks.pop("premix", None)
        if premix is not None and premix["integrated_lufs"] is not None \
                and tracks["mix"]["integrated_lufs"] is not None:
            # The stems are measured before loudness normalisation; shift them by the gain
            # the render applied so they line up with the mix.
            gain = tracks["mix"]["integrated_lufs"] - premix["integrated_lufs"]
            for name, tr in tracks.items():
                if name != "mix":
                    tr["series"] = [(t, max(ears.FLOOR_LU, lu + gain)) for t, lu in tr["series"]]
                    if tr["integrated_lufs"] is not None:
                        tr["integrated_lufs"] = round(tr["integrated_lufs"] + gain, 1)
        main = tracks[tg["main"]]
        clipped = ears.clipped_samples(media, cancelled)
        data: dict[str, Any] = {
            "target": target, "what": tg["what"], "duration_s": duration,
            "integrated_lufs": main["integrated_lufs"], "true_peak_dbtp": main["true_peak_dbtp"],
            "tracks": {n: tr["integrated_lufs"] for n, tr in tracks.items()},
            "silences": measured["silences"], "clipped_samples": clipped}
        data["transcript"], data["differences"] = self._hear(media, duration,
                                                             tg["sentences"], cancelled)
        image = ears.chart(tracks, duration, measured["silences"])
        data["summary"] = self._listen_summary(tg["what"], data)
        return Seen([jpeg(image)], data)

    def _hear(self, media: Path, duration: float, sentences: list[str] | None,
              cancelled: Callable[[], bool] | None
              ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if not self.transcripts.whisper.available:
            return {"available": False,
                    "fix": "Run `fvs whisper-download` once (about 500 MB) to hear speech."}, None
        found = self.transcripts.cached(media)
        if found is None and self.transcripts.estimate_s(duration) > INLINE_LIMIT_S:
            job = self.jobs.submit("listen", {"media": str(media), "duration_s": duration},
                                   idempotency_key=f"listen:{media}", reuse_done=False)
            return {"available": True, "status": "listening", "job_id": job["id"],
                    "next": "Wait with fvs_job_get, then call fvs_listen again."}, None
        t = found or self.transcripts.get(media, cancelled=cancelled)
        words = heard_words(t)
        transcript = {"available": True, "status": "ready", "text": t["text"],
                      "language": t["language"],
                      "segments": [{"start": s["start"], "end": s["end"], "text": s["text"]}
                                   for s in t["segments"]]}
        return transcript, ears.compare(sentences, words) if sentences is not None else None

    @staticmethod
    def _listen_summary(what: str, d: dict[str, Any]) -> str:
        parts = []
        if d["differences"] is not None:
            diff = d["differences"]
            parts.append(f"heard {diff['match']:.0%} of the script words"
                         + (f" (sentence {', '.join(map(str, diff['missing_sentences']))} "
                            "missing)" if diff["missing_sentences"] else ""))
        elif d["transcript"].get("status") == "listening":
            parts.append("transcribing in a job")
        if d["integrated_lufs"] is not None:
            parts.append(f"{d['integrated_lufs']} LUFS, true peak {d['true_peak_dbtp']} dBTP")
        parts.append(f"{len(d['silences'])} silence(s) over {ears.SILENCE_MIN_S} s")
        if d["clipped_samples"]:
            parts.append(f"{d['clipped_samples']} clipped samples")
        return f"Listened to {what}: " + "; ".join(parts) + "."

    def run_listen_job(self, ctx: JobContext) -> dict[str, Any]:
        media = Path(ctx.payload["media"])
        t = self.transcripts.get(media, cancelled=lambda: ctx.cancelled,
                                 progress=lambda p: ctx.progress(p, "Transcribing"),
                                 duration_s=ctx.payload["duration_s"])
        return {"summary": f"Transcribed {media.name}: {len(heard_words(t))} words. "
                           "Call fvs_listen again for the comparison."}
