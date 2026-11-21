"""H.264 encoders for renders: the GPU when there is one, else libx264 (RND-6).

``performance.device`` in Settings picks ``gpu`` (the default) or ``cpu``. On ``gpu`` the
first hardware encoder that passes a short test encode is used, in the order NVIDIA,
Intel, AMD, Apple. A render whose GPU encode fails is made again once with libx264.
Filters (scaling, captions, mixing) run on the CPU either way; the GPU does the encode.
"""

from __future__ import annotations

import functools

from fvs.errors import FvsError
from fvs.media.tools import run_tool

CPU_ENCODER = "libx264"
GPU_ORDER = ("h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox")

# Constant quality with no bitrate cap, at about libx264 CRF 17 ("visually lossless"),
# so the upload keeps the detail of the source clips; TikTok re-encodes it anyway.
ENCODER_ARGS: dict[str, list[str]] = {
    "libx264": ["-c:v", "libx264", "-profile:v", "high", "-preset", "fast", "-crf", "17"],
    "h264_nvenc": ["-c:v", "h264_nvenc", "-profile:v", "high", "-preset", "p6",
                   "-tune", "hq", "-rc", "vbr", "-cq", "18", "-b:v", "0",
                   "-spatial-aq", "1"],
    "h264_qsv": ["-c:v", "h264_qsv", "-profile:v", "high", "-preset", "slow",
                 "-global_quality", "18"],
    "h264_amf": ["-c:v", "h264_amf", "-profile:v", "high", "-quality", "quality",
                 "-rc", "cqp", "-qp_i", "17", "-qp_p", "19"],
    "h264_videotoolbox": ["-c:v", "h264_videotoolbox", "-profile:v", "high", "-q:v", "75"],
}


def _works(encoder: str) -> bool:
    args = ["-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "color=c=black:s=256x256:r=30:d=0.2", "-pix_fmt", "yuv420p",
            *ENCODER_ARGS[encoder], "-f", "null", "-"]
    try:
        return run_tool("ffmpeg", args, timeout=30).returncode == 0
    except (FvsError, OSError):
        return False


@functools.cache
def gpu_encoder() -> str | None:
    """The first hardware encoder that really encodes here, tested once per run."""
    return next((e for e in GPU_ORDER if _works(e)), None)


def pick_encoder(device: str) -> str:
    """The encoder for ``performance.device`` (``gpu`` or ``cpu``)."""
    return (gpu_encoder() if device == "gpu" else None) or CPU_ENCODER
