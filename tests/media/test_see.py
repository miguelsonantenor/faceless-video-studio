"""Perception and QA on real media (SEE-2–5, QA-1–3, AC-13)."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from fvs.analyze.transcribe import Whisper
from fvs.core import Core
from fvs.qa import checks as qa
from fvs.see.images import SAFE_COLOUR
from fvs.see.service import Seen
from fvs.settings import SafeZoneSettings
from tests.conftest import wait_for
from tests.fake_elevenlabs import FakeElevenLabs, sine_mp3
from tests.media.conftest import number_of, open_core
from tests.media.make_clips import SPEECH_TEXT
from tests.media.test_analysis import REAL_MODELS
from tests.media.test_render import _ready, _render

pytestmark = pytest.mark.media


def _counter_clip(out: Path) -> Path:
    """3 s at 30 fps; frame n has the grey level 32 + 2n (lossless), so a frame tells its
    own number."""
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "nullsrc=s=320x180:r=30:d=3,geq=lum='32+2*N':cb=128:cr=128",
                        "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", str(out)],
                       check=True)
    return out


def _frame_number(jpeg: bytes) -> float:
    grey = np.asarray(Image.open(io.BytesIO(jpeg)).convert("L"), dtype=float).mean()
    y = grey * 219 / 255 + 16  # back to the limited-range luma the clip was written with
    return (y - 32) / 2


@pytest.fixture(scope="module")
def score(clips: dict[str, Path], tmp_path_factory: pytest.TempPathFactory) -> Iterator[Core]:
    base = tmp_path_factory.mktemp("see")
    counter = _counter_clip(base / "made" / "counter.mp4")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FVS_HOME", str(base / "dot-fvs"))
        mp.setenv("FVS_ROOT", str(base / "FVS"))
        core = open_core(base / "dot-fvs", base / "FVS", {**clips, "counter.mp4": counter})
        core.library.scan()
        core.voice.client = FakeElevenLabs(mp3=sine_mp3).client()
        core.jobs.start()
        yield core
        core.jobs.stop(timeout=5)


@pytest.fixture(scope="module")
def rendered(score: Core) -> dict:
    n = _ready(score, ["landscape.mp4", "portrait.mp4", "phone_vfr.mov"])
    return {"n": n, **_render(score, n)}


def _img(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def test_ac13_clip_frames_are_at_the_requested_times(score: Core) -> None:
    times = [0.0, 0.5, 1.0, 1.7333, 2.5, 2.9]
    seen = score.perception.frames(source="clip", clip=number_of(score, "counter.mp4"),
                                   times=times)
    assert len(seen.images) == 6
    for t, data in zip(times, seen.images, strict=True):
        assert abs(_frame_number(data) - round(t * 30)) <= 1, t
        assert max(_img(data).size) <= 512


def test_render_frames_and_spec_stills_match(score: Core, rendered: dict) -> None:
    times = [1.0, 3.6]
    from_render = score.perception.frames(source="render", project=rendered["n"], times=times)
    from_spec = score.perception.frames(source="spec", project=rendered["n"], times=times)
    for a, b in zip(from_render.images, from_spec.images, strict=True):
        ia, ib = _img(a), _img(b)
        assert ia.size == ib.size == (288, 512)
        diff = np.abs(np.asarray(ia, float) - np.asarray(ib, float)).mean()
        assert diff < 12, diff


def test_spec_stills_are_quick(score: Core, rendered: dict) -> None:
    import time

    started = time.monotonic()
    score.perception.frames(source="spec", project=rendered["n"], count=6)
    assert time.monotonic() - started <= 3.0  # NFR-P2


def test_many_frames_become_one_sheet_with_the_safe_zone(score: Core, rendered: dict) -> None:
    seen = score.perception.frames(source="render", project=rendered["n"], count=12,
                                   safe_zone=True)
    assert len(seen.images) == 1 and max(_img(seen.images[0]).size) <= 1024
    single = _img(score.perception.frames(source="render", project=rendered["n"],
                                          times=[1.0], safe_zone=True).images[0])
    px = np.asarray(single)
    assert (np.abs(px - np.array(SAFE_COLOUR)).sum(axis=2) < 60).any()


def test_storyboard_lists_every_segment(score: Core, rendered: dict) -> None:
    proj = score.projects.get_row(rendered["n"])
    timeline = score.specs.check(proj, score.specs.current(proj)["spec"])[1]
    for source in ("spec", "render"):
        board = score.perception.storyboard(rendered["n"], source=source)
        assert [r["segment"] for r in board.data["segments"]] == \
            list(range(1, len(timeline["segments"]) + 1))
        assert board.data["segments"][0]["words"]
        assert max(_img(board.images[0]).size) <= 1024


def test_qa_report_and_contact_sheet_are_stored(score: Core, rendered: dict) -> None:
    with score.db.connect() as conn:
        row = conn.execute("SELECT qa_report, contact_sheet_path FROM renders WHERE id = ?",
                           (rendered["render_id"],)).fetchone()
    report = json.loads(row["qa_report"])
    by_id = {c["id"]: c for c in report["checks"]}
    assert list(by_id) == ["format", "tail", "loudness", "true_peak", "voice", "safe_zone",
                           "black_frozen", "glyphs"]
    for check in ("format", "tail", "loudness", "true_peak", "safe_zone", "black_frozen"):
        assert by_id[check]["status"] == "pass", by_id[check]
    assert by_id["voice"]["status"] == "warn"  # no Whisper model in the test home
    assert Path(row["contact_sheet_path"]).exists()
    assert rendered["qa"]["status"] in ("pass", "warn")


def test_captions_outside_a_moved_safe_zone_fail(score: Core, rendered: dict) -> None:
    folder = score.projects.folder(score.projects.get_row(rendered["n"]))
    ass_name = next((folder / "captions").glob("*.ass")).relative_to(folder).as_posix()
    events = qa.caption_events((folder / ass_name).read_text(encoding="utf-8"))
    boxes = qa.caption_boxes(folder, ass_name, None, qa.sample_times(events),
                             rendered["duration_s"])
    assert boxes and all(b is not None for _, b in boxes)
    assert qa.check_safe_zone(boxes, SafeZoneSettings(), captions_on=True).status == "pass"
    tight = SafeZoneSettings(top=108, bottom=900, left=60, right=120)
    assert qa.check_safe_zone(boxes, tight, captions_on=True).status == "fail"


@pytest.mark.parametrize("name,args", [
    ("fvs_frames", {"source": "spec", "times": [0.5, 1.5, 2.5], "safe_zone": True}),
    ("fvs_frames", {"source": "render", "count": 12}),
    ("fvs_storyboard", {}),
    ("fvs_listen", {"target": "render"}),
    ("fvs_clip_inspect", {"clip": 1}),
])
def test_ac11_image_tools_through_mcp(score: Core, rendered: dict, name: str,
                                      args: dict) -> None:
    import asyncio
    import time

    from fvs.mcp.registry import MAX_RESULT_BYTES
    from fvs.mcp.tools import build_registry

    if name != "fvs_clip_inspect":
        args = {**args, "project": rendered["n"]}
    started = time.monotonic()
    result = asyncio.run(build_registry().call(score, name, args))
    assert time.monotonic() - started < 20
    assert result["isError"] is False, result
    *images, text = result["content"]
    assert images and all(i["type"] == "image" and i["mimeType"] == "image/jpeg"
                          for i in images)
    assert "summary" in json.loads(text["text"]) and "structuredContent" not in result
    assert len(json.dumps(result)) < MAX_RESULT_BYTES


def _listen_ok(seen: Seen) -> None:
    assert len(seen.images) == 1 and _img(seen.images[0]).size == (1024, 300)


def test_listen_to_a_render(score: Core, rendered: dict) -> None:
    seen = score.perception.listen(project=rendered["n"], target="render")
    _listen_ok(seen)
    d = seen.data
    assert set(d["tracks"]) >= {"voice", "original", "mix"}
    assert abs(d["integrated_lufs"] + 14) <= 1.0
    assert d["tracks"]["voice"] is not None and d["tracks"]["voice"] < d["tracks"]["mix"] + 1
    assert d["transcript"]["available"] is False and "whisper-download" in \
        d["transcript"]["fix"]
    assert d["clipped_samples"] == 0


@pytest.mark.skipif(not Whisper(REAL_MODELS).available,
                    reason="Whisper model not downloaded (run `fvs whisper-download`)")
def test_ac13_listen_flags_a_removed_sentence(score: Core, clips: dict[str, Path]) -> None:
    p = score.projects.create(clips=[1], title="Listen")
    script = SPEECH_TEXT.replace(". ", ".\n") + "\nThen the whole sky lights up at once."
    score.scripts.save(p["number"], voiceover=script)
    job = score.voice.generate(p["number"])
    if "job_id" in job:
        wait_for(lambda: score.jobs.get(job["job_id"])["state"] == "done", timeout=30)
    proj = score.projects.get_row(p["number"])
    with score.db.connect() as conn:
        wav = conn.execute("SELECT audio_path FROM voiceovers WHERE id = ?",
                           (proj["voiceover_id"],)).fetchone()[0]
    # Replace the fake tone with real speech of the first two sentences only.
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i",
                    str(clips["speech.mp4"]), "-vn", "-ar", "48000", "-ac", "1", wav],
                   check=True)
    score.perception.transcripts.whisper = Whisper(REAL_MODELS)
    seen = score.perception.listen(project=p["number"], target="voiceover")
    _listen_ok(seen)
    diff = seen.data["differences"]
    assert diff["missing_sentences"] == [3], diff
    assert "sentence 3 missing" in seen.data["summary"]
