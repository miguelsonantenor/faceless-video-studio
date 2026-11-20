"""ANA-1–7 on real files: details, scenes, black/frozen, loudness, sheet, cache, jobs."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PIL import Image

import fvs.analyze.detect
import fvs.analyze.probe
import fvs.analyze.sheet
from fvs.analyze.transcribe import Whisper, best_device
from fvs.core import Core
from fvs.errors import FvsError
from tests.conftest import wait_for
from tests.media.conftest import number_of

pytestmark = pytest.mark.media
NO_TRANSCRIPT = ["scenes", "black_frozen", "loudness"]


def inspect(core: Core, name: str, extras: list[str] | None = None) -> dict:
    return core.analysis.inspect(number_of(core, name), extras or NO_TRANSCRIPT,
                                 inline_limit_s=float("inf"))


def test_details(scanned_core: Core) -> None:
    d = inspect(scanned_core, "phone_vfr.mov")["details"]
    assert (d["rotation"], d["width"], d["height"]) == (90, 720, 1280)
    assert d["orientation"] == "portrait" and d["is_vfr"] is True
    assert d["video_codec"] == "h264" and d["audio_codec"] == "aac"
    uhd = inspect(scanned_core, "uhd_4k.mp4")["details"]
    assert (uhd["width"], uhd["height"], uhd["fps"]) == (3840, 2160, 30.0)
    assert uhd["is_vfr"] is False and uhd["bit_rate"] > 0
    assert inspect(scanned_core, "nosound_8s.mp4")["details"]["has_audio"] is False


def test_scene_changes(scanned_core: Core) -> None:
    scenes = inspect(scanned_core, "landscape.mp4")["scenes"]
    assert [s["start"] for s in scenes] == pytest.approx([0.0, 4.0, 8.0], abs=0.1)
    assert scenes[-1]["end"] == pytest.approx(12.0, abs=0.05)


def test_black_and_frozen(scanned_core: Core) -> None:
    r = inspect(scanned_core, "nosound_8s.mp4")
    assert r["black"] == [{"start": pytest.approx(0, abs=0.05), "end": pytest.approx(2, abs=0.1)}]
    assert any(f["start"] == pytest.approx(5, abs=0.2) and f["end"] == pytest.approx(8, abs=0.1)
               for f in r["frozen"])
    clean = inspect(scanned_core, "landscape.mp4")
    assert clean["black"] == [] and clean["frozen"] == []


def test_loudness(scanned_core: Core) -> None:
    speech = inspect(scanned_core, "speech.mp4")["loudness"]
    assert -30 < speech["integrated_lufs"] < -10
    assert speech["true_peak_dbtp"] <= 0.5
    assert inspect(scanned_core, "nosound_8s.mp4")["loudness"] is None


def test_contact_sheet(scanned_core: Core) -> None:
    for name in ("landscape.mp4", "portrait.mp4", "uhd_4k.mp4"):
        sheet = inspect(scanned_core, name)["contact_sheet"]
        path = Path(sheet["path"])
        assert len(sheet["times"]) == 6 and path.stat().st_size < 1_000_000
        with Image.open(path) as img:
            assert img.format == "JPEG" and max(img.size) <= 1024
            if name == "portrait.mp4":
                assert img.height > img.width * 0.8  # portrait tiles


def test_results_are_cached_by_clip_hash(scanned_core: Core,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    first = inspect(scanned_core, "landscape.mp4")
    clip = scanned_core.library.get_row(number_of(scanned_core, "landscape.mp4"))
    assert Path(first["contact_sheet"]["path"]).parent.name == clip["content_hash"]

    def boom(*_a, **_k):
        raise AssertionError("FFmpeg ran again")

    for mod in (fvs.analyze.detect, fvs.analyze.probe, fvs.analyze.sheet):
        monkeypatch.setattr(mod, "run_tool", boom)
    assert inspect(scanned_core, "landscape.mp4") == first


def test_transcript_reports_missing_model_or_no_sound(scanned_core: Core,
                                                      tmp_path: Path) -> None:
    scanned_core.analyzer.whisper = Whisper(tmp_path / "no-models")
    r = inspect(scanned_core, "landscape.mp4", ["transcript"])
    assert r["transcript"]["available"] is False
    assert "fvs whisper-download" in r["transcript"]["fix"]
    r = inspect(scanned_core, "nosound_8s.mp4", ["transcript"])
    assert r["transcript"] == {"available": False, "reason": "The clip has no sound."}


def test_long_analysis_becomes_a_job(lib_core: Core) -> None:
    lib_core.library.scan()
    n = number_of(lib_core, "long_3min.mp4")
    r = lib_core.analysis.inspect(n, NO_TRANSCRIPT, inline_limit_s=0)
    assert r["status"] == "analysing" and r["job_id"] and r["details"] is None
    again = lib_core.analysis.inspect(n, NO_TRANSCRIPT, inline_limit_s=0)
    assert again["job_id"] == r["job_id"]  # no duplicate job (MCP-6)

    lib_core.jobs.start()
    wait_for(lambda: lib_core.jobs.get(r["job_id"])["state"] == "done", timeout=60)
    job = lib_core.jobs.get(r["job_id"])
    assert job["result"]["summary"].startswith(f"Analysed clip #{n}: 180.0 s landscape")
    done = lib_core.analysis.inspect(n, NO_TRANSCRIPT, inline_limit_s=0)
    assert done["status"] == "ready" and done["scenes"]


def test_estimate_sends_long_transcription_to_a_job(scanned_core: Core) -> None:
    clip = scanned_core.library.get_row(number_of(scanned_core, "long_3min.mp4"))
    assert scanned_core.analyzer.estimate_seconds(clip, {"transcript"}) > 15
    short = scanned_core.library.get_row(number_of(scanned_core, "nosound_8s.mp4"))
    assert scanned_core.analyzer.estimate_seconds(short, {"details", "scenes"}) < 15


def test_unknown_extra_is_refused(scanned_core: Core) -> None:
    with pytest.raises(FvsError, match="Unknown extras"):
        scanned_core.analysis.inspect(1, ["faces"])


def test_three_minute_clip_meets_nfr_p4(lib_core: Core) -> None:
    """NFR-P4: ≤ 10 s per minute of video, transcription not included."""
    lib_core.library.scan()
    start = time.monotonic()
    inspect(lib_core, "long_3min.mp4")
    assert time.monotonic() - start <= 30


REAL_MODELS = Path.home() / ".fvs" / "models"


@pytest.mark.skipif(not Whisper(REAL_MODELS).available,
                    reason="Whisper model not downloaded (run `fvs whisper-download`)")
def test_transcript_of_real_speech(scanned_core: Core) -> None:
    scanned_core.analyzer.whisper = Whisper(REAL_MODELS)
    t = inspect(scanned_core, "speech.mp4", ["transcript"])["transcript"]
    assert t["available"] and t["has_speech"] and t["language"] == "en"
    assert t["device"] == best_device().name
    text = t["text"].lower()
    assert "volcano" in text and "lightning" in text
    words = [w for s in t["segments"] for w in s["words"]]
    assert words and all(a["start"] <= b["start"] for a, b in zip(words, words[1:],
                                                                    strict=False))
