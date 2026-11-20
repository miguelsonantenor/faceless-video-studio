"""Voiceovers against a fake ElevenLabs (VO-1–7, AC-8). Needs FFmpeg to join audio."""

from __future__ import annotations

import json
import shutil
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest

from fvs.core import Core
from fvs.errors import ErrorCode, FvsError
from tests.conftest import make_project, wait_for
from tests.fake_elevenlabs import FakeElevenLabs, sine_mp3

pytestmark = pytest.mark.media

SCRIPT = ("The volcano makes its own lightning.\n"
          "It happens {1,000|one thousand} times a year. [pause 0.5]\n"
          "Nobody knows exactly why.")
CHARS = [36, 37, 25]  # spoken characters per sentence


@pytest.fixture
def fake() -> FakeElevenLabs:
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg not installed")
    return FakeElevenLabs(mp3=sine_mp3)


@pytest.fixture
def vcore(core: Core, fake: FakeElevenLabs) -> Iterator[Core]:
    core.voice.client = fake.client()
    core.jobs.start()
    yield core


def _scripted(core: Core, text: str = SCRIPT) -> int:
    n = make_project(core, "Volcano")["number"]
    core.scripts.save(n, voiceover=text)
    return n


def _voice(core: Core, n: int, **kw: object) -> dict:
    r = core.voice.generate(n, **kw)
    if r.get("job_id"):
        wait_for(lambda: core.jobs.get(r["job_id"])["state"] in ("done", "failed"),
                 timeout=30)
        job = core.jobs.get(r["job_id"])
        if job["error"]:
            raise FvsError(ErrorCode(job["error"]["code"]), job["error"]["message"],
                           job["error"]["fix"])
        return job["result"]
    return r


def _ledger(core: Core) -> list[dict]:
    with core.db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM cost_ledger ORDER BY id")]


def test_dry_run_reports_cost_budget_and_quota(vcore: Core, fake: FakeElevenLabs) -> None:
    n = _scripted(vcore)
    r = vcore.voice.generate(n, dry_run=True)
    assert (r["to_make"], r["chars_to_bill"], r["chars_reused"]) == (3, sum(CHARS), 0)
    assert r["budget"]["limit"] == 20_000 and r["budget"]["left"] == 20_000
    assert r["quota"] == {"available": True, "limit": 100_000, "left": 99_000,
                          "resets_at": "2026-01-01T00:00:00+00:00"}
    assert not r["over_budget"] and not fake.calls


def test_voiceover_track_timings_ledger_and_state(vcore: Core, fake: FakeElevenLabs) -> None:
    n = _scripted(vcore)
    r = _voice(vcore, n)
    assert (r["chars_billed"], r["chars_reused"], r["state"]) == (sum(CHARS), 0, "voiced")
    assert len(fake.calls) == 3 and [row["chars"] for row in _ledger(vcore)] == CHARS

    row = vcore.projects.get_row(n)
    with vcore.db.connect() as conn:
        vo = dict(conn.execute("SELECT * FROM voiceovers WHERE id = ?",
                               (row["voiceover_id"],)).fetchone())
    with wave.open(vo["audio_path"]) as w:
        assert (w.getframerate(), w.getnchannels()) == (48_000, 1)
        assert abs(w.getnframes() / 48_000 - r["duration_s"]) < 0.002
    timings = json.loads(Path(vo["timings_path"]).read_text(encoding="utf-8"))
    s1, s2, s3 = timings["sentences"]
    # Sentences follow each other; the pause after sentence 2 is exact silence.
    assert s1["start"] == 0 and abs(s2["start"] - s1["end"]) < 0.002
    assert abs(s3["start"] - s2["end"] - 0.5) < 0.002
    assert abs(r["duration_s"] - s3["end"]) < 0.002
    # The pair is one caption word timed over "one thousand", shifted by the offset.
    w = next(w for w in s2["words"] if w["word"] == "1,000")
    spoken = s2["spoken"]
    assert abs(w["start"] - (s2["start"] + spoken.index("one") * 0.05)) < 0.002
    assert [x["word"] for x in s2["words"]] == "It happens 1,000 times a year.".split()
    assert r["sentences"] == [{"i": s["i"], "start": s["start"], "end": s["end"]}
                              for s in timings["sentences"]]


def test_stitching_passes_previous_request_ids(vcore: Core, fake: FakeElevenLabs) -> None:
    _voice(vcore, _scripted(vcore, SCRIPT + "\nA fourth line.\nA fifth line."))
    assert [c.get("previous_request_ids") for c in fake.calls] == [
        None, ["req1"], ["req1", "req2"], ["req1", "req2", "req3"],
        ["req2", "req3", "req4"]]


def test_eleven_v3_is_not_stitched(vcore: Core, fake: FakeElevenLabs) -> None:
    vcore.profiles.save({"alias": "v3", "voice_id": "abc", "model_id": "eleven_v3"})
    _voice(vcore, _scripted(vcore), voice="v3")
    assert all("previous_request_ids" not in c for c in fake.calls)
    assert {c["model_id"] for c in fake.calls} == {"eleven_v3"}


def test_ac8_unchanged_script_bills_nothing(vcore: Core, fake: FakeElevenLabs) -> None:
    n = _scripted(vcore)
    first = _voice(vcore, n)
    again = vcore.voice.generate(n)
    assert again["unchanged"] and again["voiceover_id"] == first["voiceover_id"]
    assert vcore.voice.generate(n, dry_run=True)["chars_to_bill"] == 0
    assert len(fake.calls) == 3 and len(_ledger(vcore)) == 3


def test_ac8_one_changed_sentence_bills_only_that(vcore: Core, fake: FakeElevenLabs) -> None:
    n = _scripted(vcore)
    _voice(vcore, n)
    vcore.scripts.save(n, voiceover=SCRIPT.replace("exactly why", "why at all"))
    assert vcore.projects.get_row(n)["state"] == "scripted"
    dry = vcore.voice.generate(n, dry_run=True)
    assert (dry["to_make"], dry["chars_to_bill"]) == (1, 24)
    r = _voice(vcore, n)
    assert (r["chars_billed"], r["chars_reused"]) == (24, 73)
    assert [c["text"] for c in fake.calls[3:]] == ["Nobody knows why at all."]
    assert [row["chars"] for row in _ledger(vcore)] == [*CHARS, 24]


def test_ac8_over_budget_is_refused_without_the_flag(vcore: Core, fake: FakeElevenLabs) -> None:
    n = _scripted(vcore)
    vcore.settings.update({"voice.daily_budget_chars": 50})
    with pytest.raises(FvsError) as info:
        vcore.voice.generate(n)
    assert info.value.code == ErrorCode.BUDGET_EXCEEDED
    assert "allow_over_budget" in info.value.fix and not fake.calls

    r = _voice(vcore, n, allow_over_budget=True)
    assert r["chars_billed"] == sum(CHARS)
    assert [row["over_budget_approved"] for row in _ledger(vcore)] == [0, 1, 1]
    assert any("as the creator agreed" in a["summary"] for a in vcore.activity.recent(10))


def test_budget_stop_midway_loses_nothing(vcore: Core, fake: FakeElevenLabs) -> None:
    """A budget lowered for the run stops it before the next sentence; made ones stay."""
    n = _scripted(vcore)
    vcore.settings.update({"voice.daily_budget_chars": 40})
    with pytest.raises(FvsError, match="kept and will not be billed again"):
        # The plan fits in the budget only if it is checked once; check per call.
        vcore.voice._build(vcore.projects.get_row(n), vcore.profiles.get(),
                           allow_over_budget=False, source="claude")
    assert len(fake.calls) == 1 and vcore.projects.get_row(n)["state"] == "scripted"
    vcore.settings.update({"voice.daily_budget_chars": 20_000})
    r = _voice(vcore, n)
    assert (r["chars_billed"], r["chars_reused"]) == (sum(CHARS[1:]), CHARS[0])


def test_a_different_voice_makes_new_audio(vcore: Core, fake: FakeElevenLabs) -> None:
    n = _scripted(vcore)
    _voice(vcore, n)
    vcore.profiles.save({"alias": "calm", "voice_id": "other", "stability": 0.8})
    r = _voice(vcore, n, voice="calm")
    assert r["voice"] == "calm" and r["chars_billed"] == sum(CHARS)
    assert {c["path"] for c in fake.calls[3:]} == {"/v1/text-to-speech/other/with-timestamps"}


def test_voice_errors_fail_the_job_and_flag_the_project(vcore: Core,
                                                        fake: FakeElevenLabs) -> None:
    from tests.fake_elevenlabs import error

    n = _scripted(vcore)
    fake.queued = [error(401, {"status": "invalid_api_key", "message": "bad"})]
    with pytest.raises(FvsError) as info:
        _voice(vcore, n)
    assert info.value.code == ErrorCode.VOICE_AUTH
    row = vcore.projects.get_row(n)
    assert row["failed"] == 1 and row["state"] == "scripted"
    assert json.loads(row["last_error"])["code"] == "VOICE_AUTH"
    assert vcore.scripts.current(row)["version"] == 1  # the script is never lost
