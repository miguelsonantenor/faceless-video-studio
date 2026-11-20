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


def wait_for(pred: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(interval)
    raise AssertionError("condition not met in time")
