"""Plan and save an edit for real clips with a (fake) voiceover (EDT-1–5, AC-6 timeline)."""

from __future__ import annotations

import pytest

from fvs.core import Core
from tests.conftest import wait_for
from tests.fake_elevenlabs import FakeElevenLabs, sine_mp3
from tests.media.conftest import number_of

pytestmark = pytest.mark.media

SCRIPT = ("This volcano makes its own lightning.\n"
          "Ash particles rub together and build up a huge electric charge. [pause 0.4]\n"
          "Then the sky lights up.\n"
          "Scientists still argue about exactly how it starts.")


def test_plan_and_save_with_real_analysis(lib_core: Core) -> None:
    core = lib_core
    core.library.scan()
    core.voice.client = FakeElevenLabs(mp3=sine_mp3).client()
    core.jobs.start()
    clips = [number_of(core, "landscape.mp4"), number_of(core, "portrait.mp4")]
    p = core.projects.create(clips=clips, title="Volcano lightning")
    core.scripts.save(p["number"], voiceover=SCRIPT, hook="This volcano makes lightning")
    job = core.voice.generate(p["number"])
    wait_for(lambda: core.jobs.get(job["job_id"])["state"] == "done", timeout=30)

    plan = core.planner.propose(p["number"])
    assert plan["problems"] == [], plan["problems"]
    timeline = plan["timeline"]
    segs = timeline["segments"]
    assert segs[0]["out_start"] == 0 and segs[-1]["out_end"] == timeline["duration_s"]
    assert all(a["out_end"] == b["out_start"] for a, b in zip(segs, segs[1:], strict=False))
    # The landscape clip has cuts at 4 s and 8 s: no shot from it crosses a cut.
    land = [s for s in segs if s["clip"] == clips[0]]
    assert land and all(int(s["src_in"] // 4) == int((s["src_out"] - 1e-3) // 4) for s in land)
    assert {s["clip"] for s in segs} == set(clips)

    saved = core.specs.save(p["number"], spec=plan["spec"])
    assert saved["state"] == "planned" and saved["version"] == 1
    # Every sentence cue starts exactly on its sentence's slot, on the frame grid (AC-6).
    timings = core.voice.timings(core.projects.get_row(p["number"]))
    starts = {s["i"]: s["start"] for s in timings["sentences"]}
    for seg in saved["timeline"]["segments"]:
        first = seg["sentences"][0]
        if abs(seg["out_start"] - starts[first]) < 0.5:
            assert abs(seg["out_start"] - starts[first]) <= 1 / 30
        # Times are frame numbers / 30, rounded to 4 decimals (1.8667 s = frame 56).
        assert abs(seg["out_start"] * 30 - round(seg["out_start"] * 30)) < 0.005
