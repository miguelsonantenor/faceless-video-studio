from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from fvs.core import Core
from fvs.timeutil import now_iso


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated ~/.fvs and ~/FVS for each test."""
    h = tmp_path / "dot-fvs"
    monkeypatch.setenv("FVS_HOME", str(h))
    monkeypatch.setenv("FVS_ROOT", str(tmp_path / "FVS"))
    return h


@pytest.fixture
def core(home: Path) -> Iterator[Core]:
    c = Core.open(home)
    yield c
    c.jobs.stop(timeout=5)


def capture_events(core: Core, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Record every event the core publishes from now on."""
    events: list[dict] = []
    publish = core.bus.publish

    def record(event: dict) -> None:
        events.append(event)
        publish(event)

    monkeypatch.setattr(core.bus, "publish", record)
    return events


def add_clip(core: Core, name: str = "volcano_lightning.mp4", duration_s: float = 20.0) -> int:
    """Register a clip row without a real file (enough for scripts and voice)."""
    with core.db.connect() as conn:
        number = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM clips").fetchone()[0]
        conn.execute(
            "INSERT INTO clips (id, number, path, file_name, content_hash, size_bytes, "
            "mtime_ns, duration_s, width, height, fps, has_audio, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 1, ?, 1920, 1080, 30, 1, ?, ?)",
            (f"c{number}", number, f"/in/{number}-{name}", name, f"h{number}", duration_s,
             now_iso(), now_iso()))
    return number


def make_project(core: Core, title: str | None = None) -> dict:
    return core.projects.create(clips=[add_clip(core)], title=title)


def word(w: str, start: float, end: float) -> dict:
    return {"word": w, "start": start, "end": end, "emph": False}


# Three sentences; a 0.5 s pause after the second. With a 0.6 s tail the video is 4.9 s.
# Slots: [0, 1.5), [1.5, 3.2), [3.2, 4.9). Words 1–9.
TIMINGS = {"sentences": [
    {"i": 1, "start": 0.0, "end": 1.5, "caption": "The volcano erupts.",
     "spoken": "The volcano erupts.", "pause_after_s": 0.0,
     "words": [word("The", 0.05, 0.3), word("volcano", 0.3, 0.8), word("erupts.", 0.8, 1.4)]},
    {"i": 2, "start": 1.5, "end": 2.7, "caption": "It happens often.",
     "spoken": "It happens often.", "pause_after_s": 0.5,
     "words": [word("It", 1.55, 1.7), word("happens", 1.7, 2.1), word("often.", 2.1, 2.6)]},
    {"i": 3, "start": 3.2, "end": 4.4, "caption": "Nobody knows why.",
     "spoken": "Nobody knows why.", "pause_after_s": 0.0,
     "words": [word("Nobody", 3.25, 3.6), word("knows", 3.6, 3.9), word("why.", 3.9, 4.3)]},
]}


def voiced_project(core: Core, clip_numbers: list[int], timings: dict | None = None,
                   hook: str | None = "Volcanoes make lightning") -> dict:
    """A project with a script and a voiceover row, without calling ElevenLabs."""
    import json

    timings = {"voiceover_id": "vo1", "duration_s": 4.4, "cache_keys": [],
               **(timings or TIMINGS)}
    p = core.projects.create(clips=clip_numbers, title="Volcano")
    text = "\n".join(s["spoken"] for s in timings["sentences"])
    core.scripts.save(p["number"], voiceover=text, hook=hook)
    folder = core.projects.folder(core.projects.get_row(p["number"])) / "voice"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "vo1.json").write_text(json.dumps(timings), encoding="utf-8")
    with core.db.connect() as conn:
        conn.execute(
            "INSERT INTO voiceovers (id, project_id, script_version, voice_alias, audio_path, "
            "timings_path, duration_s, created_at) VALUES ('vo1', ?, 1, 'narrator', ?, ?, "
            "4.4, ?)", (p["id"], str(folder / "vo1.wav"), str(folder / "vo1.json"), now_iso()))
        conn.execute("UPDATE projects SET voiceover_id = 'vo1', state = 'voiced' WHERE id = ?",
                     (p["id"],))
    return core.projects.get_row(p["number"])


def wait_for(pred: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(interval)
    raise AssertionError("condition not met in time")
