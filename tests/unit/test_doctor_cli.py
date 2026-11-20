from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fvs.cli import app
from fvs.config import Config, ensure_token
from fvs.doctor import Check, run_doctor, worst


def test_doctor_reports_every_area() -> None:
    names = [c.name for c in run_doctor(Config(), online=False)]
    for expected in ("FFmpeg", "ffprobe", "libass (captions)", "libx264", "Hardware encoder",
                     "Bundled fonts", "Whisper model", "ElevenLabs key", "Disk space",
                     "Claude Desktop", "fvs command"):
        assert expected in names


def test_missing_key_is_a_warning_with_a_fix() -> None:
    check = next(c for c in run_doctor(Config(), online=False) if c.name == "ElevenLabs key")
    assert check.status == "warn" and "Settings" in check.fix


@pytest.mark.media
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg not installed")
def test_ffmpeg_on_this_machine_is_usable() -> None:
    checks = {c.name: c for c in run_doctor(Config(), online=False)}
    for name in ("FFmpeg", "ffprobe", "libass (captions)", "libx264"):
        assert checks[name].status == "ok", checks[name]


def test_worst() -> None:
    assert worst([Check("a", "ok", ""), Check("b", "warn", "")]) == "warn"
    assert worst([Check("a", "fail", ""), Check("b", "warn", "")]) == "fail"
    assert worst([Check("a", "ok", "")]) == "ok"


def test_cli_token_and_doctor(home: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["token"])
    assert r.exit_code == 0 and r.stdout.strip() == ensure_token(home)
    r = runner.invoke(app, ["doctor", "--no-online"])
    assert r.exit_code in (0, 1) and "Overall:" in r.stdout
