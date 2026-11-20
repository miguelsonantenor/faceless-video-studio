"""Audio as samples: decode any media with FFmpeg, write 16-bit WAV."""

from __future__ import annotations

import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fvs.atomic import atomic_path
from fvs.errors import ErrorCode, FvsError
from fvs.media.tools import run_tool


def decode_pcm(media: Path, rate: int, cancelled: Callable[[], bool] | None = None) -> Any:
    """The first audio track as mono float32 samples at ``rate`` Hz (a numpy array)."""
    import numpy as np

    res = run_tool("ffmpeg", ["-hide_banner", "-loglevel", "error", "-i", str(media),
                              "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(rate),
                              "-f", "f32le", "-"], cancelled=cancelled, timeout=600)
    if res.returncode != 0:
        raise FvsError(ErrorCode.CLIP_UNREADABLE,
                       f"The sound of {media.name} could not be decoded: {res.stderr_tail(2)}",
                       "Check that the file plays with sound; re-export it if it does not.")
    return np.frombuffer(res.stdout, dtype=np.float32)


def write_wav(path: Path, samples: Any, rate: int) -> None:
    """Write mono float samples (−1…1) as 16-bit PCM WAV, atomically."""
    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    with atomic_path(path) as tmp, wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
