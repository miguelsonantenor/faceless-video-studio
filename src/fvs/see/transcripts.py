"""Whisper transcripts of voiceovers, renders and clips, cached by file (SEE-5, QA-1).

A file's transcript is kept under ``cache/transcripts`` keyed by its path, size and
modified time, so listening to a render after its QA check costs nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fvs.analyze.transcribe import MODEL_SIZE, Whisper
from fvs.atomic import write_text_atomic


def heard_words(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for seg in transcript["segments"] for w in seg["words"]]


class Transcripts:
    def __init__(self, whisper: Whisper, cache_dir: Path) -> None:
        self.whisper = whisper
        self.cache_dir = cache_dir

    def _path(self, media: Path) -> Path:
        st = media.stat()
        key = f"{media.resolve()}|{st.st_size}|{st.st_mtime_ns}|{MODEL_SIZE}"
        return self.cache_dir / f"{hashlib.sha1(key.encode()).hexdigest()}.json"

    def cached(self, media: Path) -> dict[str, Any] | None:
        path = self._path(media)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def estimate_s(self, duration_s: float) -> float:
        return duration_s / self.whisper.device.x_realtime

    def get(self, media: Path, *, cancelled: Callable[[], bool] | None = None,
            progress: Callable[[float], None] | None = None,
            duration_s: float | None = None) -> dict[str, Any]:
        found = self.cached(media)
        if found is not None:
            return found
        result = self.whisper.transcribe(media, cancelled=cancelled, progress=progress,
                                         duration_s=duration_s)
        write_text_atomic(self._path(media), json.dumps(result))
        return result
