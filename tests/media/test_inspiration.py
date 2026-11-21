"""Inspiration sets on real videos (INS-1–3): profile, look sheet, guide and planner."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from fvs.core import Core
from fvs.mcp.tools import GuideArgs, guide
from tests.conftest import wait_for
from tests.media.conftest import link_or_copy

pytestmark = pytest.mark.media


def test_profile_look_sheet_guide_and_pacing(lib_core: Core, clips: dict[str, Path]) -> None:
    core = lib_core
    folder = core.config.folders.expanded().inspiration / "volcano"
    for name in ("landscape.mp4", "speech.mp4"):
        link_or_copy(clips[name], folder / name)
    core.jobs.start()

    first, sheet = core.inspiration.profile("volcano", inline_limit_s=0)
    assert first["status"] == "analysing" and sheet is None  # long work is a job (ANA-7)
    wait_for(lambda: core.jobs.get(first["job_id"])["state"] in ("done", "failed"), 120)
    assert core.jobs.get(first["job_id"])["state"] == "done"

    profile, sheet = core.inspiration.profile("volcano")
    assert profile["status"] == "ready" and profile["videos"] == 2
    landscape = next(v for v in profile["per_video"] if v["file_name"] == "landscape.mp4")
    assert landscape["shots"] == 3 and landscape["median_shot_s"] == pytest.approx(4, abs=0.2)
    assert landscape["cuts_per_s"] == pytest.approx(2 / 12, abs=0.02)
    speech = next(v for v in profile["per_video"] if v["file_name"] == "speech.mp4")
    if core.whisper.available:
        assert speech["words_per_minute"] and "volcano" in speech["hook"].lower()
    assert profile["measures"]["length_s"]["min"] <= profile["measures"]["length_s"]["max"]
    assert sheet is not None and max(Image.open(io.BytesIO(sheet)).size) <= 1024  # MCP-4

    core.settings.update({"defaults.inspiration_set": "volcano"})
    brief = guide(core, GuideArgs(sections=["inspiration"]))["inspiration"]
    assert brief["set"] == "volcano" and brief["measures"]["median_shot_s"]
    assert not list(folder.glob("*.json")) and not list(folder.glob("*.jpg"))  # read-only
