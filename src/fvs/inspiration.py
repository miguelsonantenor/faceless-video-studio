"""Inspiration sets: example videos the creator likes (INS-1–3, BUILD_PLAN 6.1).

The creator drops videos into the inspiration folder (``~/FVS/inspiration``). Each
subfolder is one set (``storytime/``, ``perfume/``); videos loose in the folder itself
form the set ``main``. FVS never writes there (read-only area, like the input folder),
and inspiration videos are never clips: they have no clip number, so no project or Edit
Spec can use their footage.

A set's style profile is built from the same analysis cache as clips (by content hash):
length, shot length and cuts per second, time to the first word, words per minute,
sentence length, the hook and the ending, the share of the video with speech, and
loudness, each as the median and range across the set plus the value per video. A look
sheet shows three frames of each video, so Claude can judge what is not measured
(caption look, framing, colour, transitions). The profile is cached by a fingerprint of
the videos and their notes.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from fvs.activity import ActivityLog, Source
from fvs.analyze.analyzer import ANALYZER_VERSION, Analyzer
from fvs.analyze.service import INLINE_LIMIT_S
from fvs.analyze.sheet import BG, grab_frame, label_image
from fvs.atomic import atomic_path, write_text_atomic
from fvs.errors import ErrorCode, FvsError
from fvs.jobs import JobContext, JobQueue
from fvs.library import VIDEO_EXTS, file_hash
from fvs.settings import SettingsService
from fvs.store import Database
from fvs.timeutil import now_iso

ROOT_SET = "main"  # videos directly in the inspiration folder
PROFILE_VERSION = 1
EXTRAS = frozenset({"scenes", "loudness", "transcript"})
FRAME_AT = (0.1, 0.5, 0.9)  # start, middle, near the end
PER_SHEET = 12
PER_ROW = 3
CELL = (112, 200)  # one frame, 9:16
GAP = 6
MAX_SIDE = 1024
SHOT_RANGE = (0.8, 6.0)  # the planner's target shot length stays in here (INS-3)
MEASURES = ("length_s", "median_shot_s", "cuts_per_s", "first_word_s", "words_per_minute",
            "words_per_sentence", "hook_words", "hook_seconds", "speech_share",
            "loudness_lufs")


def _spread(values: list[float | None]) -> dict[str, float] | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {"median": round(statistics.median(vals), 2), "min": round(min(vals), 2),
            "max": round(max(vals), 2)}


def _sentences(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = [[]]
    for w in words:
        out[-1].append(w)
        if w["word"].rstrip("\"')”’").endswith((".", "!", "?")):
            out.append([])
    return [s for s in out if s]


def _text(words: list[dict[str, Any]]) -> str:
    return " ".join(w["word"] for w in words).strip()


def measure(details: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """The style measures of one video, from its probe and analysis."""
    duration = float(details["duration_s"]) or 0.001
    shots = [s["end"] - s["start"] for s in result.get("scenes") or []
             if s["end"] > s["start"]] or [duration]
    out: dict[str, Any] = {
        "length_s": round(duration, 1), "shots": len(shots),
        "median_shot_s": round(statistics.median(shots), 2),
        "cuts_per_s": round((len(shots) - 1) / duration, 2),
        "loudness_lufs": (result.get("loudness") or {}).get("integrated_lufs"),
        "first_word_s": None, "words_per_minute": None, "words_per_sentence": None,
        "hook": None, "hook_words": None, "hook_seconds": None, "ending": None,
        "speech_share": 0.0,
    }
    t = result.get("transcript")
    segments = t.get("segments", []) if isinstance(t, dict) and t.get("available") else []
    words = [w for seg in segments for w in seg.get("words", [])]
    if words:
        span = words[-1]["end"] - words[0]["start"]
        sentences = _sentences(words)
        first = sentences[0]
        out.update(
            first_word_s=round(words[0]["start"], 2),
            words_per_minute=round(len(words) / span * 60) if span > 0 else None,
            words_per_sentence=statistics.median(len(s) for s in sentences),
            hook=_text(first), hook_words=len(first),
            hook_seconds=round(first[-1]["end"] - first[0]["start"], 1),
            ending=_text(sentences[-1]),
            speech_share=round(min(1.0, sum(s["end"] - s["start"] for s in segments)
                                   / duration), 2))
    return out


class Inspiration:
    def __init__(self, db: Database, *, folder: Path, cache_root: Path, analyzer: Analyzer,
                 jobs: JobQueue, settings: SettingsService, activity: ActivityLog) -> None:
        self.db = db
        self.folder = folder
        self.cache_root = cache_root
        self.analyzer = analyzer
        self.jobs = jobs
        self.settings = settings
        self.activity = activity
        jobs.register("inspiration", self.run_job)

    # ---- the folder ----------------------------------------------------------------

    def _set_folders(self) -> dict[str, Path]:
        found = {ROOT_SET: self.folder} if self.folder.is_dir() else {}
        if self.folder.is_dir():
            for sub in sorted(self.folder.iterdir()):
                if sub.is_dir() and not sub.name.startswith("."):
                    found[sub.name] = sub
        return found

    def scan(self) -> list[dict[str, Any]]:
        """Bring the sets and videos in line with the folder; return every set."""
        folders = self._set_folders()
        with self.db.transaction() as conn:
            sets = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM inspiration_sets")}
            for name in set(sets) - set(folders):
                conn.execute("DELETE FROM inspiration_sets WHERE id = ?", (sets[name]["id"],))
            for name, folder in folders.items():
                if name not in sets:
                    sets[name] = {"id": uuid.uuid4().hex, "name": name}
                    conn.execute("INSERT INTO inspiration_sets (id, name, folder, created_at) "
                                 "VALUES (?, ?, ?, ?)",
                                 (sets[name]["id"], name, str(folder), now_iso()))
                self._sync_videos(conn, sets[name]["id"], folder)
        return [self.get(name) for name in folders]

    def _sync_videos(self, conn: Any, set_id: str, folder: Path) -> None:
        known = {r["path"]: dict(r) for r in conn.execute(
            "SELECT * FROM inspiration_videos WHERE set_id = ?", (set_id,))}
        notes = {r["content_hash"]: r["note"] for r in known.values()}
        files = sorted(p for p in folder.iterdir()
                       if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
        keep: set[str] = set()
        for path in files:
            st = path.stat()
            row = known.get(str(path))
            if row and (row["size_bytes"], row["mtime_ns"]) == (st.st_size, st.st_mtime_ns):
                keep.add(row["content_hash"])
                continue
            digest = file_hash(path)
            keep.add(digest)
            conn.execute(
                "INSERT INTO inspiration_videos (set_id, content_hash, path, size_bytes, "
                "mtime_ns, note, added_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (set_id, content_hash) DO UPDATE SET path = excluded.path, "
                "size_bytes = excluded.size_bytes, mtime_ns = excluded.mtime_ns",
                (set_id, digest, str(path), st.st_size, st.st_mtime_ns,
                 notes.get(digest, ""), now_iso()))
        for row in known.values():
            if row["content_hash"] not in keep:
                conn.execute("DELETE FROM inspiration_videos WHERE set_id = ? AND "
                             "content_hash = ?", (set_id, row["content_hash"]))

    def get(self, name: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM inspiration_sets WHERE name = ?",
                               (name,)).fetchone()
            if row is None:
                names = ", ".join(r["name"] for r in conn.execute(
                    "SELECT name FROM inspiration_sets ORDER BY name")) or "none yet"
                raise FvsError(ErrorCode.NOT_FOUND, f"There is no inspiration set {name!r}.",
                               f"Use one of the sets ({names}). A set is a subfolder of the "
                               f"inspiration folder ({self.folder}).")
            videos = [dict(r) for r in conn.execute(
                "SELECT content_hash, path, note FROM inspiration_videos WHERE set_id = ?",
                (row["id"],))]
        videos.sort(key=lambda v: Path(v["path"]).name.lower())
        for k, v in enumerate(videos, 1):
            v["number"] = k
            v["file_name"] = Path(v["path"]).name
        default = self.settings.get().defaults.inspiration_set
        return {"id": row["id"], "name": name, "folder": row["folder"], "videos": videos,
                "is_default": name == default}

    def choose(self, name: str | None) -> str | None:
        """The set to use: the one named, else the default, else the only one with videos."""
        if name:
            return name
        default = self.settings.get().defaults.inspiration_set
        if default:
            return default
        with_videos = [s["name"] for s in self.scan() if s["videos"]]
        return with_videos[0] if len(with_videos) == 1 else None

    def set_note(self, name: str, video: int | str, note: str,
                 source: Source = "creator") -> dict[str, Any]:
        s = self.get(name)
        match = [v for v in s["videos"] if str(video) in (str(v["number"]), v["file_name"])
                 or (len(str(video)) >= 8 and v["content_hash"].startswith(str(video)))]
        if not match:
            raise FvsError(ErrorCode.NOT_FOUND, f"Set {name!r} has no video {video!r}.",
                           f"Use a video number from 1 to {len(s['videos'])}.")
        note = note.strip()[:500]
        with self.db.transaction() as conn:
            conn.execute("UPDATE inspiration_videos SET note = ? WHERE set_id = ? AND "
                         "content_hash = ?", (note, s["id"], match[0]["content_hash"]))
        self.activity.add(f"Noted on inspiration video {match[0]['file_name']} ({name}): "
                          f"“{note}”." if note else f"Cleared the note on "
                          f"{match[0]['file_name']} ({name}).", source=source,
                          tool="fvs_inspiration" if source == "claude" else None)
        return self.get(name)

    # ---- analysis ------------------------------------------------------------------

    def _clip(self, video: dict[str, Any]) -> dict[str, Any]:
        """What the analyzer needs, as for a clip (but this is never a clip)."""
        clip = {"content_hash": video["content_hash"], "path": video["path"]}
        info = self.analyzer.details(clip)
        return {**clip, "duration_s": info.duration_s, "width": info.width,
                "height": info.height, "has_audio": info.has_audio}

    def _missing(self, clip: dict[str, Any]) -> set[str]:
        missing = self.analyzer.missing(clip, EXTRAS)
        if not self.analyzer.whisper.available:
            missing.discard("transcript")
        return missing

    def _fingerprint(self, s: dict[str, Any]) -> str:
        blob = json.dumps([sorted((v["content_hash"], v["note"]) for v in s["videos"]),
                           ANALYZER_VERSION, PROFILE_VERSION])
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def _cache(self, s: dict[str, Any]) -> Path:
        return self.cache_root / s["id"]

    def cached_profile(self, name: str | None) -> dict[str, Any] | None:
        """The profile if it is already built for the set as it is now; never computes."""
        if not name:
            return None
        try:
            s = self.get(name)
        except FvsError:
            return None
        path = self._cache(s) / f"{self._fingerprint(s)}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def profile(self, name: str | None, *, page: int = 1,
                inline_limit_s: float = INLINE_LIMIT_S) -> tuple[dict[str, Any], bytes | None]:
        """(profile, look sheet JPEG); or a job to wait for when analysis would be long."""
        self.scan()
        chosen = self.choose(name)
        if chosen is None:
            sets = [{"name": s["name"], "videos": len(s["videos"])} for s in self.scan()]
            return {"status": "choose_set", "sets": sets, "folder": str(self.folder),
                    "summary": (f"{len(sets)} inspiration set(s); name one with set, or ask "
                                "the creator to pick a default in Settings."
                                if any(s["videos"] for s in sets) else
                                f"No inspiration videos yet. The creator can drop example "
                                f"videos into {self.folder} (a subfolder per style).")}, None
        s = self.get(chosen)
        if not s["videos"]:
            raise FvsError(ErrorCode.NOT_FOUND, f"The inspiration set {chosen!r} has no "
                           "videos.", f"Put example videos in {s['folder']}, then try again.")
        cached = self.cached_profile(chosen)
        if cached is None:
            clips = [self._clip(v) for v in s["videos"]]
            estimate = sum(self.analyzer.estimate_seconds(c, self._missing(c)) for c in clips)
            if estimate > inline_limit_s:
                job = self.jobs.submit("inspiration", {"set": chosen},
                                       idempotency_key=f"inspiration:{self._fingerprint(s)}",
                                       reuse_done=False)
                return {"status": "analysing", "set": chosen, "job_id": job["id"],
                        "estimate_s": round(estimate),
                        "summary": f"Analysing the {len(clips)} video(s) of the inspiration "
                                   f"set {chosen!r} (about {estimate:.0f} s); wait with "
                                   "fvs_job_get, then call fvs_inspiration again."}, None
            cached = self._build(s)
        return cached, self.look_sheet(s, page)

    def run_job(self, ctx: JobContext) -> dict[str, Any]:
        s = self.get(ctx.payload["set"])
        profile = self._build(s, ctx)
        return {"set": s["name"], "summary": profile["summary"]}

    def _build(self, s: dict[str, Any], ctx: JobContext | None = None) -> dict[str, Any]:
        per_video = []
        for k, video in enumerate(s["videos"]):
            clip = self._clip(video)
            if ctx is not None:
                ctx.progress(k / len(s["videos"]), f"Analysing {video['file_name']}")
            result = self.analyzer.analyze(
                clip, EXTRAS, cancelled=(lambda: ctx.cancelled) if ctx else None)
            per_video.append({"number": video["number"], "file_name": video["file_name"],
                              "note": video["note"], **measure(result["details"], result)})
        spread = {m: _spread([v[m] for v in per_video]) for m in MEASURES}
        shot = spread["median_shot_s"]
        n = len(per_video)
        summary = (f"Inspiration set {s['name']!r}: {n} video(s)"
                   + (f", median length {spread['length_s']['median']:.0f} s"
                      if spread["length_s"] else "")
                   + (f", median shot {shot['median']:.1f} s" if shot else "")
                   + (f", {spread['words_per_minute']['median']:.0f} words a minute"
                      if spread["words_per_minute"] else "") + ".")
        profile = {"status": "ready", "set": s["name"], "videos": n,
                   "measures": spread, "per_video": per_video,
                   "target_shot_s": round(min(max(shot["median"], SHOT_RANGE[0]),
                                              SHOT_RANGE[1]), 2) if shot else None,
                   "sheets": (n + PER_SHEET - 1) // PER_SHEET,
                   "not_measured": "caption font and animation, music, transitions, colour: "
                                   "judge them from the look sheet",
                   "summary": summary}
        write_text_atomic(self._cache(s) / f"{self._fingerprint(s)}.json",
                          json.dumps(profile, indent=1, ensure_ascii=False))
        return profile

    # ---- the look sheet --------------------------------------------------------------

    def look_sheet(self, s: dict[str, Any], page: int = 1) -> bytes:
        """Three frames of each video (start, middle, end), 12 videos a page."""
        pages = max(1, (len(s["videos"]) + PER_SHEET - 1) // PER_SHEET)
        if not 1 <= page <= pages:
            raise FvsError(ErrorCode.SPEC_INVALID, f"Page {page} does not exist.",
                           f"Use a page from 1 to {pages}.")
        out = self._cache(s) / f"{self._fingerprint(s)}_p{page}.jpg"
        if out.exists():
            return out.read_bytes()
        videos = s["videos"][(page - 1) * PER_SHEET:page * PER_SHEET]
        cw, ch = CELL
        group_w = len(FRAME_AT) * cw + (len(FRAME_AT) - 1) * 2
        rows = (len(videos) + PER_ROW - 1) // PER_ROW
        cols = min(PER_ROW, len(videos))
        sheet = Image.new("RGB", (cols * group_w + (cols + 1) * GAP * 2,
                                  rows * ch + (rows + 1) * GAP), BG)
        for i, video in enumerate(videos):
            clip = self._clip(video)
            x0 = GAP * 2 + (i % PER_ROW) * (group_w + GAP * 2)
            y = GAP + (i // PER_ROW) * (ch + GAP)
            scale = min(cw / max(clip["width"], 1), ch / max(clip["height"], 1))
            size = (max(2, round(clip["width"] * scale)), max(2, round(clip["height"] * scale)))
            for j, at in enumerate(FRAME_AT):
                cell = Image.new("RGB", CELL, (0, 0, 0))
                frame = grab_frame(Path(video["path"]), clip["duration_s"] * at, size)
                if frame is not None:
                    cell.paste(frame, ((cw - size[0]) // 2, (ch - size[1]) // 2))
                if j == 0:
                    label_image(cell, str(video["number"]))
                sheet.paste(cell, (x0 + j * (cw + 2), y))
        if max(sheet.size) > MAX_SIDE:
            sheet.thumbnail((MAX_SIDE, MAX_SIDE))
        with atomic_path(out) as tmp:
            sheet.save(tmp, format="JPEG", quality=82)
        return out.read_bytes()

    def thumb(self, name: str, content_hash: str) -> bytes:
        """A small middle frame of one video, for Settings → Inspiration."""
        s = self.get(name)
        video = next((v for v in s["videos"] if v["content_hash"] == content_hash), None)
        if video is None:
            raise FvsError(ErrorCode.NOT_FOUND, "That inspiration video is not there any "
                           "more.", "Rescan the inspiration folder.")
        out = self._cache(s) / f"thumb_{content_hash[:24]}.jpg"
        if not out.exists():
            clip = self._clip(video)
            img = grab_frame(Path(video["path"]), clip["duration_s"] / 2, CELL) or \
                Image.new("RGB", CELL, (0, 0, 0))
            with atomic_path(out) as tmp:
                img.save(tmp, format="JPEG", quality=80)
        return out.read_bytes()
