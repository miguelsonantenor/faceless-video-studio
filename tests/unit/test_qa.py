"""The eight QA checks, each with a measured value, a limit and a fix (QA-1, QA-2, AC-4,
AC-7)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fvs.analyze.probe import Probe
from fvs.compile.ass import build_ass
from fvs.qa import checks as qa
from fvs.settings import SafeZoneSettings
from fvs.spec.models import Captions
from fvs.spec.templates import CAPTION_STYLES
from tests.conftest import TIMINGS

GOOD = Probe(duration_s=5.5, start_s=0, width=1080, height=1920, rotation=0, fps=30.0,
             is_vfr=False, video_codec="h264", pix_fmt="yuv420p", bit_rate=None,
             has_audio=True, audio_codec="aac", sample_rate=48_000, channels=2,
             container="mov,mp4")


def test_format() -> None:
    assert qa.check_format(GOOD).status == "pass"
    sixty = qa.check_format(GOOD, 60)  # GOOD is 30 fps; a 60 fps render must be 60
    assert sixty.status == "fail" and sixty.limit.startswith("h264 1080×1920 60 fps")
    bad = qa.check_format(Probe(**{**GOOD.__dict__, "width": 720}))
    assert bad.status == "fail" and "720" in bad.measured and bad.fix


def test_tail_between_0_3_and_1_s() -> None:
    assert qa.check_tail(5.5, 4.9).status == "pass"
    late = qa.check_tail(7.0, 4.9)
    assert late.status == "fail" and "end.tail_seconds" in late.fix
    assert qa.check_tail(5.0, 4.9).status == "fail"


def test_loudness_and_true_peak() -> None:
    assert qa.check_loudness(-14.6, -14).status == "pass"
    assert qa.check_loudness(-15.6, -14).status == "warn"
    loud = qa.check_loudness(-9.0, -14)
    assert loud.status == "fail" and "audio." in loud.fix
    assert qa.check_loudness(None, -14).status == "fail"
    assert qa.check_peak(-1.2, -1).status == "pass"
    assert qa.check_peak(-0.7, -1).status == "warn"
    assert qa.check_peak(0.3, -1).status == "fail"


def test_voice_complete() -> None:
    ok = qa.check_voice({"match": 0.97, "script_words": 30, "missing_sentences": []})
    assert ok.status == "pass"
    gone = qa.check_voice({"match": 0.7, "script_words": 30, "missing_sentences": [2]})
    assert gone.status == "fail" and "sentence(s) 2" in gone.measured
    unknown = qa.check_voice(None, "no model")
    assert unknown.status == "warn" and unknown.measured == "not checked"


def _ass() -> str:
    return build_ass(timings=TIMINGS, captions=Captions(), style=CAPTION_STYLES["bold_yellow"],
                     font="Arial Black", hook_text="Volcano {lightning}", hook_seconds=2.0,
                     end_s=4.9, safe=SafeZoneSettings())


def test_caption_events_and_sample_times() -> None:
    events = qa.caption_events(_ass())
    assert events[0]["style"] == "hook" and events[0]["text"] == "VOLCANO (LIGHTNING)"
    assert all("{" not in e["text"] for e in events)
    times = qa.sample_times(events)
    assert times == sorted(times) and len(times) <= qa.MAX_CAPTION_SAMPLES
    many = [{"start": k, "end": k + 1} for k in range(100)]
    assert len(qa.sample_times(many)) == qa.MAX_CAPTION_SAMPLES


def test_safe_zone() -> None:
    safe = SafeZoneSettings()
    inside = [(1.0, (300, 1400, 800, 1500))]
    assert qa.check_safe_zone(inside, safe, captions_on=True).status == "pass"
    outside = qa.check_safe_zone([*inside, (2.0, (30, 1400, 800, 1500))], safe,
                                 captions_on=True)
    assert outside.status == "fail" and "at 2.00 s" in outside.measured
    assert "captions." in outside.fix
    assert qa.check_safe_zone([], safe, captions_on=False).status == "pass"
    assert qa.check_safe_zone([(1.0, None)], safe, captions_on=True).status == "warn"


def test_black_and_frozen_name_the_segment() -> None:
    timeline = {"segments": [
        {"index": 0, "clip": 4, "video_start": 0, "hold_s": 0},
        {"index": 1, "clip": 7, "video_start": 2.0, "hold_s": 1.2}]}
    assert qa.check_still([{"start": 0, "end": 0.4}], [], timeline).status == "pass"
    black = qa.check_still([{"start": 0.2, "end": 1.0}], [], timeline)
    assert black.status == "fail" and "timeline.segments[0]" in black.fix
    held = qa.check_still([], [{"start": 2.5, "end": 3.4}], timeline)
    assert "segments[1]" in held.fix and "loop" in held.fix


@pytest.mark.skipif(sys.platform != "win32", reason="uses a Windows system font")
def test_missing_glyphs_are_listed() -> None:
    font = qa.font_file(Path("no-such-folder"), "Arial Black")
    assert font is not None
    assert qa.check_glyphs("ÉCLAIR ÇA", font, "Arial Black").status == "pass"
    bad = qa.check_glyphs("FIRE \U0001F525", font, "Arial Black")
    assert bad.status == "fail" and "\U0001F525" in bad.measured and "overrides" in bad.fix


def test_a_missing_font_file_is_a_warning() -> None:
    assert qa.check_glyphs("A", None, "Nope").status == "warn"


def test_overall_is_the_worst() -> None:
    checks = [qa.Check("a", "A", "pass", "", ""), qa.Check("b", "B", "warn", "", "")]
    assert qa.overall(checks) == "warn"
    assert qa.overall([*checks, qa.Check("c", "C", "fail", "", "")]) == "fail"
