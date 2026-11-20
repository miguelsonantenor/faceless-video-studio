"""ANA-1: duration, resolution, frame rate, rotation, codecs, bitrate, sound."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from fvs.errors import ErrorCode, FvsError
from fvs.media.tools import run_tool

PROBE_TIMEOUT_S = 30


@dataclass
class Probe:
    duration_s: float
    start_s: float  # container start time; analysis times are relative to it
    width: int  # as displayed, after rotation
    height: int
    rotation: int  # 0, 90, 180 or 270 degrees
    fps: float  # average frame rate
    is_vfr: bool
    video_codec: str
    pix_fmt: str | None
    bit_rate: int | None
    has_audio: bool
    audio_codec: str | None
    sample_rate: int | None
    channels: int | None
    container: str

    @property
    def orientation(self) -> str:
        if self.height > self.width:
            return "portrait"
        return "landscape" if self.width > self.height else "square"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["orientation"] = self.orientation
        return d


def _rate(text: str | None) -> float:
    if not text or text in ("0/0", "0"):
        return 0.0
    try:
        return float(Fraction(text))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _rotation(stream: dict[str, Any]) -> int:
    rot: float | None = None
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            rot = float(side["rotation"])
    if rot is None and "rotate" in (stream.get("tags") or {}):
        rot = float(stream["tags"]["rotate"])
    if rot is None:
        return 0
    return int(round(rot / 90.0) * 90) % 360


def parse_probe(data: dict[str, Any]) -> Probe:
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise ValueError("no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})

    duration = float(fmt.get("duration") or video.get("duration") or 0.0)
    rotation = _rotation(video)
    cw, ch = int(video.get("width", 0)), int(video.get("height", 0))
    w, h = (ch, cw) if rotation in (90, 270) else (cw, ch)
    avg = _rate(video.get("avg_frame_rate"))
    real = _rate(video.get("r_frame_rate"))
    fps = avg or real
    # ffprobe reports r_frame_rate as the lowest rate that can represent every timestamp;
    # a clear gap from the average means the frames are not evenly spaced.
    is_vfr = bool(avg and real and abs(avg - real) / real > 0.01)
    bit_rate = fmt.get("bit_rate") or video.get("bit_rate")
    return Probe(
        duration_s=round(duration, 3),
        start_s=round(float(fmt.get("start_time") or 0.0), 3),
        width=w,
        height=h,
        rotation=rotation,
        fps=round(fps, 3),
        is_vfr=is_vfr,
        video_codec=video.get("codec_name", "unknown"),
        pix_fmt=video.get("pix_fmt"),
        bit_rate=int(bit_rate) if bit_rate else None,
        has_audio=audio is not None,
        audio_codec=audio.get("codec_name") if audio else None,
        sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        channels=int(audio["channels"]) if audio and audio.get("channels") else None,
        container=fmt.get("format_name", "unknown"),
    )


def probe(path: Path) -> Probe:
    res = run_tool("ffprobe", ["-v", "error", "-print_format", "json", "-show_format",
                               "-show_streams", str(path)], timeout=PROBE_TIMEOUT_S)
    try:
        if res.returncode != 0:
            raise ValueError(res.stderr_tail(3) or "ffprobe failed")
        return parse_probe(json.loads(res.stdout.decode("utf-8", "replace")))
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise FvsError(
            ErrorCode.CLIP_UNREADABLE,
            f"{path.name} could not be read as a video ({exc}).",
            "Check that the file plays in a video player; re-export it as MP4 (H.264) "
            "if it does not.",
        ) from None
