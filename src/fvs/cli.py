"""The ``fvs`` command: serve, stop, doctor, open, token, mcp."""

from __future__ import annotations

import os
import signal
import sys
import time
import webbrowser
from pathlib import Path

import httpx
import typer

from fvs.config import HOST, ensure_config, ensure_token
from fvs.doctor import run_doctor, worst

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Faceless Video Studio: the local core for Claude-made TikTok videos.")

_ICON = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
STOP_GRACE_S = 5  # open connections (the web app's live stream) get this long to close
STOP_WAIT_S = 20.0  # how long `fvs stop` waits before ending the process


@app.command()
def serve(
    dev_origin: list[str] = typer.Option(  # noqa: B008
        [], help="Extra allowed Origin for web app development, e.g. http://localhost:5173"),
) -> None:
    """Start the core: API, /mcp, web app and job workers, on 127.0.0.1."""
    import uvicorn

    from fvs.api import create_app
    from fvs.core import Core

    core = Core.open()
    api = create_app(core, extra_origins=dev_origin)
    # An open web app keeps a live-update stream open; without a limit, stopping would
    # wait for it forever.
    server = uvicorn.Server(uvicorn.Config(
        api, host=HOST, port=core.config.port, log_config=None, access_log=False,
        timeout_graceful_shutdown=STOP_GRACE_S))
    api.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()


@app.command()
def stop(force: bool = typer.Option(
        False, "--force", help="Stop even while jobs run; they continue at the next start.")
         ) -> None:
    """Stop the running core, its job workers and the web app."""
    config = ensure_config()
    with httpx.Client(base_url=f"http://{HOST}:{config.port}", timeout=10,
                      headers={"Authorization": f"Bearer {ensure_token()}"}) as client:
        try:
            resp = client.get("/api/status")
        except httpx.HTTPError:
            typer.echo("FVS is not running.")
            return
        status = resp.json()
        if resp.status_code != 200 or status.get("core") != "running":
            typer.echo(f"Something else answers on port {config.port}: "
                       f"{status.get('message', resp.status_code)}", err=True)
            raise typer.Exit(1)
        if status["active_jobs"] and not force:
            typer.echo(f"{status['active_jobs']} job(s) are queued or running (renders, "
                       "voiceovers). Wait for them, or run `fvs stop --force`: they are "
                       "picked up again the next time FVS starts.", err=True)
            raise typer.Exit(1)
        try:
            client.post("/api/shutdown")
        except httpx.HTTPError:
            pass  # it may already be going
        deadline = time.monotonic() + STOP_WAIT_S
        while time.monotonic() < deadline:
            try:
                client.get("/api/health", timeout=1)
            except httpx.HTTPError:
                typer.echo("FVS stopped.")
                return
            time.sleep(0.3)
        os.kill(status["pid"], signal.SIGTERM)  # last resort: it did not stop by itself
        typer.echo(f"FVS did not stop within {STOP_WAIT_S:.0f} s, so it was ended.")


@app.command()
def doctor(online: bool = typer.Option(True, help="Test the ElevenLabs key online.")) -> None:
    """Check FFmpeg, encoders, fonts, Whisper, the ElevenLabs key, disk and Claude Desktop."""
    checks = run_doctor(ensure_config(), online=online)
    for c in checks:
        typer.echo(f"[{_ICON[c.status]}] {c.name}: {c.detail}")
        if c.fix and c.status != "ok":
            typer.echo(f"        fix: {c.fix}")
    overall = worst(checks)
    typer.echo(f"\nOverall: {overall.upper()}")
    raise typer.Exit(1 if overall == "fail" else 0)


@app.command("open")
def open_web() -> None:
    """Open the web app in the browser, signed in with the local session token."""
    config = ensure_config()
    token = ensure_token()
    webbrowser.open(f"http://{HOST}:{config.port}/auth?t={token}")


@app.command()
def token() -> None:
    """Print the local session token (for `claude mcp add ... --header`)."""
    typer.echo(ensure_token())


@app.command()
def key(
    from_env: bool = typer.Option(
        False, "--from-env", help="Take ELEVENLABS_API_KEY from the environment or from "
        "--env-file (where `npx @elevenlabs/cli onboard` saves it) instead of asking."),
    env_file: Path = typer.Option(Path(".env"), help="The .env file --from-env reads."),  # noqa: B008
) -> None:
    """Test the ElevenLabs API key and save it in ~/.fvs/config.toml (readable only by you).
    The key is never printed."""
    from pydantic import SecretStr

    from fvs.config import key_from_env, save_config
    from fvs.errors import FvsError
    from fvs.voice.elevenlabs import verify_key

    config = ensure_config()
    if from_env:
        value = key_from_env(env_file)
        if value is None:
            typer.echo(f"No ELEVENLABS_API_KEY in the environment or in {env_file}.", err=True)
            raise typer.Exit(1)
    else:
        value = typer.prompt("ElevenLabs API key", hide_input=True).strip()
    try:
        verify_key(value)
    except FvsError as exc:
        typer.echo(f"{exc.message} {exc.fix}", err=True)
        raise typer.Exit(1) from None
    config.elevenlabs_api_key = SecretStr(value)
    path = save_config(config)
    typer.echo(f"Saved the key ending {config.api_key_last4} to {path}. "
               "Restart `fvs serve` if it is running.")


@app.command("whisper-download")
def whisper_download() -> None:
    """Download the Whisper speech model (small, int8, about 500 MB) into ~/.fvs/models."""
    from fvs.analyze.transcribe import Whisper
    from fvs.config import fvs_home

    w = Whisper(fvs_home() / "models")
    if w.available:
        typer.echo(f"Already installed: {w.model_path()}")
        return
    typer.echo("Downloading the Whisper 'small' model from Hugging Face…")
    typer.echo(f"Installed: {w.download()}")


@app.command()
def scan(subfolders: bool = typer.Option(False, help="Also scan subfolders.")) -> None:
    """Register new clips from the input folder."""
    from fvs.core import Core

    core = Core.open(configure_logs=False)
    result = core.library.scan(subfolders=subfolders, source="creator")
    typer.echo(result.summary())
    for c in result.new:
        typer.echo(f"  #{c['number']}  {c['file_name']}  {c['duration_s']:.1f} s  "
                   f"{c['width']}×{c['height']}  {'sound' if c['has_audio'] else 'no sound'}")
    for s in result.skipped:
        typer.echo(f"  skipped {s['file']}: {s['reason']}")


@app.command()
def inspect(clip: str, transcript: bool = typer.Option(True, help="Include speech.")) -> None:
    """Analyse one clip (by number or ID) and print the result as JSON."""
    import json

    from fvs.analyze.analyzer import EXTRAS
    from fvs.core import Core

    core = Core.open(configure_logs=False)
    extras = [e for e in EXTRAS if transcript or e != "transcript"]
    result = core.analysis.inspect(clip, extras, inline_limit_s=float("inf"))
    typer.echo(json.dumps(result, indent=1, ensure_ascii=False))


@app.command()
def mcp() -> None:
    """The stdio MCP server Claude Desktop starts; it forwards to the core's /mcp."""
    from fvs.mcp.proxy import run

    raise typer.Exit(run())


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
