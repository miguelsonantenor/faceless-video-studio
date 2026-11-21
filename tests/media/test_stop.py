"""`fvs stop` ends a real core, even while the web app holds a live stream open."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from fvs.cli import app
from fvs.config import Config, ensure_token, save_config

pytestmark = pytest.mark.media


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_fvs_stop_ends_a_running_core(home: Path) -> None:
    port = _free_port()
    save_config(Config(port=port), home)
    token = ensure_token(home)
    proc = subprocess.Popen([sys.executable, "-m", "fvs", "serve"], env=os.environ.copy(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        auth = {"Authorization": f"Bearer {token}"}
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base}/api/health", headers=auth, timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        # The web app's live stream, left open as a browser tab would.
        def listen() -> None:
            try:
                with httpx.stream("GET", f"{base}/api/events", headers=auth,
                                  timeout=None) as r:
                    for _ in r.iter_lines():
                        pass
            except httpx.HTTPError:
                pass

        threading.Thread(target=listen, daemon=True).start()
        time.sleep(0.5)
        started = time.monotonic()
        r = CliRunner().invoke(app, ["stop"])
        assert r.exit_code == 0 and "FVS stopped." in r.output, r.output
        assert proc.wait(timeout=15) is not None
        assert time.monotonic() - started < 15
        assert "not running" in CliRunner().invoke(app, ["stop"]).output
    finally:
        if proc.poll() is None:
            proc.kill()
