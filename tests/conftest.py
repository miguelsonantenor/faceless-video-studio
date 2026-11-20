from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from fvs.core import Core


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


def wait_for(pred: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(interval)
    raise AssertionError("condition not met in time")
