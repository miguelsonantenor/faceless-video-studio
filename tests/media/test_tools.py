"""FFmpeg children are never left running, whatever goes wrong (NFR-S4)."""

from __future__ import annotations

import subprocess

import pytest

from fvs.media import tools
from fvs.media.tools import run_tool

pytestmark = pytest.mark.media

LONG = ["-hide_banner", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=60",
        "-progress", "pipe:1", "-f", "null", "-"]


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen]:
    procs: list[subprocess.Popen] = []
    real = subprocess.Popen

    def recording(*a: object, **k: object) -> subprocess.Popen:
        p = real(*a, **k)
        procs.append(p)
        return p

    monkeypatch.setattr(tools.subprocess, "Popen", recording)
    return procs


def test_a_failing_progress_callback_does_not_block_ffmpeg(started: list) -> None:
    def broken(line: str) -> None:
        raise ValueError("bad progress handler")

    res = run_tool("ffmpeg", ["-hide_banner", "-f", "lavfi", "-i",
                              "testsrc2=s=320x240:r=30:d=2", "-progress", "pipe:1",
                              "-f", "null", "-"], on_line=broken, timeout=60)
    assert res.returncode == 0 and started[0].poll() is not None


@pytest.mark.parametrize("streaming", [False, True])
def test_an_error_while_waiting_kills_the_child(started: list, streaming: bool) -> None:
    def explode() -> bool:
        raise RuntimeError("the job went away")

    with pytest.raises(RuntimeError):
        run_tool("ffmpeg", LONG, cancelled=explode,
                 on_line=(lambda _line: None) if streaming else None)
    assert started[0].poll() is not None  # killed, not left encoding for a minute
    assert started[0].stdout.closed and started[0].stderr.closed
