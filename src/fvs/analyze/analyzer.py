"""The Analyzer: every clip analysis, cached by clip hash (ANA-1–7).

Cache layout, one folder per clip content hash (DS 6.2)::

    cache/<content_hash>/probe.json        ANA-1 details
    cache/<content_hash>/analysis.json     scenes, black, frozen, loudness (one pass)
    cache/<content_hash>/transcript.json   Whisper words
    cache/<content_hash>/contact_sheet.jpg + sheet.json

Each file carries ``ANALYZER_VERSION``; raising it makes old results be recomputed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fvs.analyze.detect import detect
from fvs.analyze.probe import Probe, probe
from fvs.analyze.sheet import make_sheet
from fvs.analyze.transcribe import Whisper
from fvs.atomic import write_text_atomic
from fvs.errors import FvsError

ANALYZER_VERSION = 2  # 2: probe details no longer store coded width/height
EXTRAS = ("scenes", "black_frozen", "loudness", "transcript")
DETECT_EXTRAS = frozenset({"scenes", "black_frozen", "loudness"})

# Rough speeds used to decide whether analysis runs inline or as a job (ANA-7).
DECODE_1080P_X_REALTIME = 8.0     # the detection pass decodes 1080p at ~8× real time
SHEET_SECONDS = 2.0


class Analyzer:
    def __init__(self, cache_root: Path, whisper: Whisper) -> None:
        self.cache_root = cache_root
        self.whisper = whisper

    # ---- cache helpers -------------------------------------------------------------

    def folder(self, clip: dict[str, Any]) -> Path:
        return self.cache_root / clip["content_hash"]

    def _read(self, clip: dict[str, Any], name: str) -> dict[str, Any] | None:
        path = self.folder(clip) / name
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if data.get("version") == ANALYZER_VERSION else None

    def _write(self, clip: dict[str, Any], name: str, data: dict[str, Any]) -> None:
        write_text_atomic(self.folder(clip) / name,
                          json.dumps({"version": ANALYZER_VERSION, **data}, indent=1))

    # ---- what is there, what is missing --------------------------------------------

    def missing(self, clip: dict[str, Any], extras: set[str] | frozenset[str]) -> set[str]:
        out: set[str] = set()
        if self._read(clip, "probe.json") is None:
            out.add("details")
        if self._read(clip, "sheet.json") is None or not (
                self.folder(clip) / "contact_sheet.jpg").exists():
            out.add("contact_sheet")
        if extras & DETECT_EXTRAS and self._read(clip, "analysis.json") is None:
            out |= extras & DETECT_EXTRAS
        if "transcript" in extras and clip.get("has_audio") and \
                self._read(clip, "transcript.json") is None:
            out.add("transcript")
        return out

    def estimate_seconds(self, clip: dict[str, Any], parts: set[str]) -> float:
        duration = float(clip.get("duration_s") or 0.0)
        pixels = float((clip.get("width") or 1920) * (clip.get("height") or 1080))
        est = 0.0
        if "details" in parts:
            est += 0.5
        if "contact_sheet" in parts:
            est += SHEET_SECONDS * max(1.0, pixels / (1920 * 1080))
        if parts & DETECT_EXTRAS:
            est += duration * (pixels / (1920 * 1080)) / DECODE_1080P_X_REALTIME
        if "transcript" in parts:
            est += duration / self.whisper.device.x_realtime
        return round(est, 1)

    # ---- analysis ------------------------------------------------------------------

    def details(self, clip: dict[str, Any]) -> Probe:
        cached = self._read(clip, "probe.json")
        if cached is not None:
            data = {k: v for k, v in cached["probe"].items() if k != "orientation"}
            return Probe(**data)
        info = probe(Path(clip["path"]))
        self._write(clip, "probe.json", {"probe": info.to_dict()})
        return info

    def analyze(
        self,
        clip: dict[str, Any],
        extras: set[str] | frozenset[str] = frozenset(EXTRAS),
        *,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[float, str], None] | None = None,
    ) -> dict[str, Any]:
        """Compute whatever is missing for ``extras`` and return the full result."""
        def step(f: float, note: str) -> None:
            if progress is not None:
                progress(f, note)

        path = Path(clip["path"])
        step(0.0, "Reading clip details")
        info = self.details(clip)

        if extras & DETECT_EXTRAS and self._read(clip, "analysis.json") is None:
            step(0.1, "Finding scenes, black and frozen stretches, loudness")
            found = detect(path, duration_s=info.duration_s, start_s=info.start_s,
                           has_audio=info.has_audio, cancelled=cancelled)
            self._write(clip, "analysis.json", found.to_dict())

        if self._read(clip, "sheet.json") is None or not (
                self.folder(clip) / "contact_sheet.jpg").exists():
            step(0.5, "Making the contact sheet")
            times = make_sheet(path, self.folder(clip) / "contact_sheet.jpg",
                               width=info.width, height=info.height,
                               duration_s=info.duration_s, cancelled=cancelled)
            self._write(clip, "sheet.json", {"times": times})

        if "transcript" in extras and info.has_audio and \
                self._read(clip, "transcript.json") is None:
            step(0.6, "Transcribing speech in the clip")
            try:
                text = self.whisper.transcribe(
                    path, cancelled=cancelled, duration_s=info.duration_s,
                    progress=lambda f: step(0.6 + 0.4 * f, "Transcribing speech in the clip"))
            except FvsError:
                pass  # model missing: reported as unavailable by result(), not a failure
            else:
                self._write(clip, "transcript.json", {"transcript": text})
        step(1.0, "Done")
        return self.result(clip, extras)

    def result(self, clip: dict[str, Any],
               extras: set[str] | frozenset[str] = frozenset(EXTRAS)) -> dict[str, Any]:
        """Everything cached for the clip, limited to ``extras``."""
        out: dict[str, Any] = {}
        p = self._read(clip, "probe.json")
        out["details"] = p["probe"] if p else None
        a = self._read(clip, "analysis.json")
        if "scenes" in extras:
            out["scenes"] = a["scenes"] if a else None
        if "black_frozen" in extras:
            out["black"] = a["black"] if a else None
            out["frozen"] = a["frozen"] if a else None
        if "loudness" in extras:
            out["loudness"] = a["loudness"] if a else None
        if "transcript" in extras:
            if not clip.get("has_audio"):
                out["transcript"] = {"available": False, "reason": "The clip has no sound."}
            else:
                t = self._read(clip, "transcript.json")
                if t is not None:
                    out["transcript"] = {"available": True, **t["transcript"]}
                elif not self.whisper.available:
                    out["transcript"] = {
                        "available": False,
                        "reason": "The Whisper speech model is not installed.",
                        "fix": "Run `fvs whisper-download` once, then inspect the clip again."}
                else:
                    out["transcript"] = None
        s = self._read(clip, "sheet.json")
        sheet = self.folder(clip) / "contact_sheet.jpg"
        out["contact_sheet"] = ({"path": str(sheet), "times": s["times"]}
                                if s and sheet.exists() else None)
        return out
