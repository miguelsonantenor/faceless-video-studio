"""Full renders of real clips (RND-1–5, AC-2, AC-3, AC-5, AC-6, NFR-P1)."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from fvs.analyze.detect import detect
from fvs.core import Core
from tests.conftest import wait_for
from tests.fake_elevenlabs import FakeElevenLabs, sine_mp3
from tests.media.conftest import number_of, open_core

pytestmark = pytest.mark.media

SCRIPT = ("This volcano makes its own lightning.\n"
          "Ash rubs together and builds a huge electric charge. [pause 0.4]\n"
          "Then the whole sky lights up at once.")


def _ready(core: Core, files: list[str], **plan_changes: object) -> int:
    """A project with a voiceover and a saved planned spec."""
    p = core.projects.create(clips=[number_of(core, f) for f in files], title="Volcano")
    n = p["number"]
    core.scripts.save(n, voiceover=SCRIPT, hook="This volcano makes lightning")
    job = core.voice.generate(n)
    if "job_id" in job:  # otherwise every sentence came from the cache, made inline
        wait_for(lambda: core.jobs.get(job["job_id"])["state"] == "done", timeout=30)
    plan = core.planner.propose(n, changes=plan_changes or None)
    assert plan["problems"] == [], plan["problems"]
    core.specs.save(n, spec=plan["spec"])
    return n


def _render(core: Core, n: int) -> dict:
    started = core.renders.start([n])["items"][0]
    assert started["status"] == "rendering"
    wait_for(lambda: core.jobs.get(started["job_id"])["state"] in ("done", "failed",
                                                                    "cancelled"), timeout=180)
    job = core.jobs.get(started["job_id"])
    assert job["state"] == "done", job["error"]
    return job["result"]


@pytest.fixture(scope="module")
def rcore(clips: dict[str, Path], tmp_path_factory: pytest.TempPathFactory) -> Iterator[Core]:
    base = tmp_path_factory.mktemp("render")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FVS_HOME", str(base / "dot-fvs"))
        mp.setenv("FVS_ROOT", str(base / "FVS"))
        core = open_core(base / "dot-fvs", base / "FVS", clips)
        core.library.scan()
        core.voice.client = FakeElevenLabs(mp3=sine_mp3).client()
        core.jobs.start()
        yield core
        core.jobs.stop(timeout=5)


@pytest.fixture(scope="module")
def rendered(rcore: Core) -> dict:
    n = _ready(rcore, ["landscape.mp4", "portrait.mp4", "phone_vfr.mov"])
    return {"n": n, **_render(rcore, n)}


def _ffprobe(path: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of",
                          "json", path], capture_output=True, check=True, text=True).stdout
    return json.loads(out)


def test_ac2_output_format(rendered: dict) -> None:
    info = _ffprobe(rendered["path"])
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (v["codec_name"], v["profile"], v["pix_fmt"]) == ("h264", "High", "yuv420p")
    assert (v["width"], v["height"], v["r_frame_rate"]) == (1080, 1920, "30/1")
    assert (a["codec_name"], a["sample_rate"]) == ("aac", "48000")
    data = Path(rendered["path"]).read_bytes()
    assert data.find(b"moov") < data.find(b"mdat")  # fast start


def test_duration_matches_the_timeline(rcore: Core, rendered: dict) -> None:
    proj = rcore.projects.get_row(rendered["n"])
    timeline = rcore.specs.check(proj, rcore.specs.current(proj)["spec"])[1]
    assert abs(rendered["duration_s"] - timeline["duration_s"]) <= 2 / 30


def test_ac3_loudness_and_true_peak(rendered: dict) -> None:
    loud = detect(Path(rendered["path"]), duration_s=rendered["duration_s"], start_s=0,
                  has_audio=True).loudness
    assert abs(loud["integrated_lufs"] + 14) <= 1.0, loud
    assert loud["true_peak_dbtp"] <= -1.0, loud


def test_state_render_row_command_log_and_reuse(rcore: Core, rendered: dict) -> None:
    proj = rcore.projects.get_row(rendered["n"])
    assert proj["state"] == "claude_check" and proj["render_id"] == rendered["render_id"]
    with rcore.db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM renders WHERE id = ?",
                                (rendered["render_id"],)).fetchone())
    log = json.loads(row["command_log"])
    assert "libx264" in log["render"] and "loudnorm" in log["measure"]
    assert Path(log["log"]).exists()
    again = rcore.renders.start([rendered["n"]])
    assert again["items"][0]["status"] == "reused"
    assert not list(Path(rendered["path"]).parent.glob("*.fvs-tmp"))


def test_nfr_p1_render_time(rendered: dict) -> None:
    # NFR-P1: at most 60 s for a 60 s video, so at most the video's own length.
    assert rendered["render_seconds"] <= max(10.0, rendered["duration_s"]), rendered


def _band_db(path: str, start: float, length: float, freq: int) -> float:
    """RMS level of a narrow band; four stacked band-passes keep the 300 Hz voice out."""
    band = ",".join([f"bandpass=f={freq}:width_type=h:w=8"] * 4)
    err = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
         "-i", path, "-af", f"{band},astats=metadata=0", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    return float(re.findall(r"RMS level dB:\s*(-?[\d.]+|-inf)", err)[-1])


def test_ac5_original_sound_drops_under_the_voice(rcore: Core) -> None:
    n = _ready(rcore, ["landscape.mp4"])  # 440 Hz tone; the fake voice is 300 Hz
    result = _render(rcore, n)
    timings = rcore.voice.timings(rcore.projects.get_row(n))
    first = timings["sentences"][0]["words"]
    last_word = timings["sentences"][-1]["words"][-1]["end"]
    speaking = _band_db(result["path"], first[1]["start"], 0.5, 440)
    tail = _band_db(result["path"], last_word + 0.2, 0.3, 440)  # after the duck ramps
    assert tail - speaking >= 10, (speaking, tail)


def test_cancel_removes_the_partial_file(rcore: Core) -> None:
    n = _ready(rcore, ["long_3min.mp4"])
    job = rcore.renders.start([n])["items"][0]["job_id"]
    wait_for(lambda: (rcore.jobs.get(job)["progress"] or 0) > 0.1
             or rcore.jobs.get(job)["state"] != "running", timeout=60)
    rcore.jobs.cancel(job)
    wait_for(lambda: rcore.jobs.get(job)["state"] in ("cancelled", "done"), timeout=30)
    if rcore.jobs.get(job)["state"] == "cancelled":
        folder = rcore.projects.folder(rcore.projects.get_row(n)) / "renders"
        assert not list(folder.glob("*.fvs-tmp"))
        assert rcore.projects.get_row(n)["state"] == "rendering"
