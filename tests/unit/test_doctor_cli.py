from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from fvs.cli import app
from fvs.config import Config, ensure_token, key_from_env, load_config
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


KEY = "sk_onboard_TESTKEY_0123456789abcd"


def test_key_from_env_prefers_the_environment(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("# made by the ElevenLabs CLI\nOTHER=1\n"
                   f'export ELEVENLABS_API_KEY="{KEY}"  # comment\n', encoding="utf-8")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert key_from_env(env) == KEY
    assert key_from_env(tmp_path / "missing.env") is None
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_from_the_environment_0000")
    assert key_from_env(env) == "sk_from_the_environment_0000"


def test_cli_key_from_env_tests_saves_and_never_prints(
        home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    env = tmp_path / ".env"
    runner = CliRunner()
    r = runner.invoke(app, ["key", "--from-env", "--env-file", str(env)])
    assert r.exit_code == 1 and "No ELEVENLABS_API_KEY" in r.output

    env.write_text(f"ELEVENLABS_API_KEY={KEY}\n", encoding="utf-8")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(401))
    r = runner.invoke(app, ["key", "--from-env", "--env-file", str(env)])
    assert r.exit_code == 1 and "did not accept" in r.output
    assert load_config(home).elevenlabs_api_key is None  # nothing saved

    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json={}))
    r = runner.invoke(app, ["key", "--from-env", "--env-file", str(env)])
    assert r.exit_code == 0 and "ending abcd" in r.output and KEY not in r.output
    assert load_config(home).elevenlabs_api_key.get_secret_value() == KEY
