"""One real ElevenLabs call (about 40 characters). Opt-in: set FVS_LIVE_ELEVENLABS=1 and
save a key with `fvs key`. Only the key is read from the real ~/.fvs/config.toml."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fvs.config import load_config
from fvs.core import Core
from tests.conftest import make_project, wait_for

pytestmark = [pytest.mark.media,
              pytest.mark.skipif(os.environ.get("FVS_LIVE_ELEVENLABS") != "1",
                                 reason="live ElevenLabs test; set FVS_LIVE_ELEVENLABS=1")]


def test_live_voiceover(core: Core) -> None:
    key = load_config(Path.home() / ".fvs").elevenlabs_api_key  # the real config, not tmp
    if key is None:
        pytest.skip("no ElevenLabs key saved (run `fvs key`)")
    core.config.elevenlabs_api_key = key
    core.jobs.start()
    n = make_project(core, "Live test")["number"]
    core.scripts.save(n, voiceover="Volcanoes make lightning.\nNobody knows why.")
    job = core.voice.generate(n)
    wait_for(lambda: core.jobs.get(job["job_id"])["state"] in ("done", "failed"), timeout=90)
    result = core.jobs.get(job["job_id"])
    assert result["state"] == "done", result["error"]
    assert result["result"]["chars_billed"] > 0 and result["result"]["duration_s"] > 1
