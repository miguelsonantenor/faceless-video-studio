"""``fvs doctor``: the start-up self-check (DS 10.6).

Each check says ok, warn or fail, what it found, and how to fix it. Hardware encoders
are listed but not trial-encoded here; that comes with RND-6 in phase 4.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

import httpx

from fvs.config import Config
from fvs.voice.elevenlabs import SUBSCRIPTION_URL

Status = Literal["ok", "warn", "fail"]
GB = 1024**3
MIN_FFMPEG_MAJOR = 7
HW_ENCODERS = ("h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox")


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def claude_desktop_config_path() -> Path:
    if sys.platform == "win32":
        # The Microsoft Store build of Claude reads its config from its package folder,
        # not from %APPDATA%; prefer that folder when the Store build is installed.
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        store = sorted((local / "Packages").glob("Claude_*/LocalCache/Roaming/Claude"))
        roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Claude"
        return (store[0] if store else roaming) / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def _run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15, check=False,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_ffmpeg(ffmpeg: str = "ffmpeg") -> list[Check]:
    path = shutil.which(ffmpeg)
    if path is None:
        fix = ("Install FFmpeg 7 or newer (on Windows the gyan.dev 'full' build) and put its "
               "bin folder on the PATH.")
        return [Check("FFmpeg", "fail", "Not found on the PATH", fix)]
    checks: list[Check] = []
    out = _run([path, "-hide_banner", "-version"])
    first = out.stdout.splitlines()[0] if out and out.stdout else ""
    m = re.search(r"ffmpeg version n?(\d+)\.(\d+)", first)
    if m is None:
        checks.append(Check("FFmpeg", "warn", f"Found at {path}, version unknown ({first!r})",
                            "Use FFmpeg 7 or newer."))
    elif int(m.group(1)) < MIN_FFMPEG_MAJOR:
        checks.append(Check("FFmpeg", "fail", f"Version {m.group(1)}.{m.group(2)} is too old",
                            "Install FFmpeg 7 or newer."))
    else:
        checks.append(Check("FFmpeg", "ok", f"Version {m.group(1)}.{m.group(2)} at {path}"))

    probe = shutil.which("ffprobe")
    checks.append(Check("ffprobe", "ok", f"Found at {probe}") if probe else
                  Check("ffprobe", "fail", "Not found on the PATH",
                        "ffprobe ships with FFmpeg; put the same bin folder on the PATH."))

    filters = _run([path, "-hide_banner", "-filters"])
    ftext = filters.stdout if filters else ""
    has_ass = re.search(r"\ssubtitles\s", ftext) and re.search(r"\sass\s", ftext)
    checks.append(Check("libass (captions)", "ok", "subtitles and ass filters available")
                  if has_ass else
                  Check("libass (captions)", "fail", "FFmpeg was built without libass",
                        "Install an FFmpeg build with libass (gyan.dev 'full' or 'essentials')."))

    encoders = _run([path, "-hide_banner", "-encoders"])
    etext = encoders.stdout if encoders else ""
    checks.append(Check("libx264", "ok", "H.264 software encoder available")
                  if re.search(r"\slibx264\s", etext) else
                  Check("libx264", "fail", "FFmpeg was built without libx264",
                        "Install an FFmpeg build with libx264."))
    hw = [e for e in HW_ENCODERS if re.search(rf"\s{e}\s", etext)]
    checks.append(Check("Hardware encoder", "ok",
                        f"Listed by FFmpeg: {', '.join(hw)} (tested when used, phase 4)" if hw
                        else "None listed; renders use libx264"))
    return checks


def check_fonts() -> Check:
    try:
        fonts_dir = resources.files("fvs") / "assets_bundled" / "fonts"
        fonts = [p.name for p in fonts_dir.iterdir()
                 if p.name.lower().endswith((".ttf", ".otf"))]
    except (FileNotFoundError, NotADirectoryError):
        fonts = []
    if fonts:
        return Check("Bundled fonts", "ok", f"{len(fonts)} font file(s)")
    return Check("Bundled fonts", "warn", "No bundled fonts yet",
                 "Fonts are added with the caption work (WP4).")


def check_whisper() -> Check:
    if importlib.util.find_spec("faster_whisper") is None:
        return Check("Whisper model", "fail", "faster-whisper is not installed",
                     "Reinstall FVS with its dependencies (`uv sync`).")
    import ctranslate2

    from fvs.analyze.transcribe import CPU, MODEL_SIZE, Whisper, best_device
    from fvs.config import fvs_home

    whisper = Whisper(fvs_home() / "models")
    if whisper.model_path() is None:
        return Check("Whisper model", "warn", f"The {MODEL_SIZE} model is not downloaded",
                     "Run `fvs whisper-download` once (about 500 MB) to transcribe speech.")
    device = best_device()
    detail = f"Installed ({MODEL_SIZE}), runs on the {device.label}"
    if device is CPU and ctranslate2.get_cuda_device_count() > 0:
        return Check("Whisper model", "warn",
                     f"{detail}: an NVIDIA GPU is present but its CUDA libraries are not",
                     "Run `uv sync --extra gpu` (about 1.25 GB) to transcribe about 7× faster.")
    return Check("Whisper model", "ok", detail)


def check_elevenlabs(config: Config, *, online: bool) -> Check:
    if config.elevenlabs_api_key is None:
        return Check("ElevenLabs key", "warn", "No key saved",
                     "Paste your ElevenLabs API key in Settings.")
    if not online:
        return Check("ElevenLabs key", "ok", f"Key ending {config.api_key_last4} (not tested)")
    try:
        resp = httpx.get(SUBSCRIPTION_URL, timeout=10,
                         headers={"xi-api-key": config.elevenlabs_api_key.get_secret_value()})
    except httpx.HTTPError:
        return Check("ElevenLabs key", "warn", "Could not reach ElevenLabs",
                     "Check the internet connection and run the check again.")
    if resp.status_code == 200:
        data = resp.json()
        left = int(data.get("character_limit", 0)) - int(data.get("character_count", 0))
        return Check("ElevenLabs key", "ok", f"Works; {left:,} characters left this period")
    if resp.status_code == 401:
        return Check("ElevenLabs key", "fail", "ElevenLabs rejected the key",
                     "Replace the key in Settings with a current one from your ElevenLabs "
                     "account page.")
    return Check("ElevenLabs key", "warn", f"ElevenLabs answered {resp.status_code}",
                 "Try again later.")


def check_disk(config: Config) -> Check:
    target = config.folders.expanded().workspace
    while not target.exists() and target.parent != target:
        target = target.parent
    free = shutil.disk_usage(target).free
    detail = f"{free / GB:.0f} GB free on the workspace drive"
    if free < 5 * GB:
        return Check("Disk space", "fail", detail, "Free up space; FVS needs at least 20 GB.")
    if free < 20 * GB:
        return Check("Disk space", "warn", detail, "FVS works best with 20 GB or more free.")
    return Check("Disk space", "ok", detail)


def check_claude_desktop() -> Check:
    path = claude_desktop_config_path()
    if not path.exists():
        return Check("Claude Desktop", "warn", "Claude Desktop config not found",
                     "Install Claude Desktop, then use Settings → Add to Claude Desktop.")
    try:
        servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
    except (OSError, json.JSONDecodeError):
        return Check("Claude Desktop", "warn", f"Could not read {path}",
                     "Check that the file is valid JSON.")
    if "fvs" in servers:
        return Check("Claude Desktop", "ok", "FVS is in the Claude Desktop config")
    return Check("Claude Desktop", "warn", "FVS is not in the Claude Desktop config yet",
                 "Use Settings → Add to Claude Desktop, or paste the config shown there.")


def check_fvs_on_path() -> Check:
    found = shutil.which("fvs")
    if found:
        return Check("fvs command", "ok", f"Found at {found}")
    return Check("fvs command", "warn", "The fvs command is not on the PATH",
                 "Install it with `uv tool install .` so Claude Desktop can start `fvs mcp`.")


def run_doctor(config: Config, *, online: bool = True) -> list[Check]:
    return [
        *check_ffmpeg(),
        check_fonts(),
        check_whisper(),
        check_elevenlabs(config, online=online),
        check_disk(config),
        check_claude_desktop(),
        check_fvs_on_path(),
    ]


def worst(checks: list[Check]) -> Status:
    statuses = {c.status for c in checks}
    return "fail" if "fail" in statuses else "warn" if "warn" in statuses else "ok"
