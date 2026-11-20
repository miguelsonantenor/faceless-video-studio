"""``fvs mcp``: the stdio transport Claude Desktop starts (MCP-10; DS 8.1).

It reads ``~/.fvs/token``, starts the core (``fvs serve``) in the background if it is not
running, then forwards each newline-delimited JSON-RPC message from stdin to ``/mcp`` and
writes the answers to stdout. Messages are forwarded in parallel, so a waiting
``fvs_job_get`` never holds up other calls. Nothing but JSON-RPC goes to stdout.

The core keeps running when the chat closes, so renders and batches carry on (ADR-3).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import IO, Any

import httpx

from fvs.config import HOST, ensure_config, ensure_token
from fvs.errors import ErrorCode

START_WAIT_S = 30.0
UNAVAILABLE_FIX = ("Run `fvs doctor` in a terminal to see what is wrong, or start the core "
                   "with `fvs serve`.")


def core_is_up(client: httpx.Client) -> bool:
    try:
        return client.get("/api/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def start_core(client: httpx.Client) -> bool:
    """Start ``fvs serve`` detached from this process and wait until it answers."""
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "fvs", "serve"], **kwargs)  # noqa: S603
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        if core_is_up(client):
            return True
        time.sleep(0.3)
    return False


class Proxy:
    def __init__(self, client: httpx.Client, out: IO[bytes]) -> None:
        self.client = client
        self.out = out
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()

    def write(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        with self._lock:
            self.out.write(data)
            self.out.flush()

    def _unavailable(self, msg: dict[str, Any], why: str) -> None:
        if "id" not in msg or "method" not in msg:
            return
        if msg["method"] == "tools/call":
            body = {"code": ErrorCode.CORE_UNAVAILABLE.value, "message": why,
                    "fix": UNAVAILABLE_FIX}
            self.write({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "content": [{"type": "text", "text": json.dumps(body)}], "isError": True}})
        else:
            self.write({"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32000, "message": f"{why} {UNAVAILABLE_FIX}"}})

    def _post(self, line: bytes, msg: dict[str, Any]) -> None:
        with self.client.stream("POST", "/mcp", content=line, headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}) as resp:
            if resp.status_code == 202:
                return
            if resp.headers.get("content-type", "").startswith("text/event-stream"):
                for row in resp.iter_lines():
                    if row.startswith("data:"):
                        self.write(json.loads(row[5:]))
                return
            resp.read()
            data = resp.json()
            if "jsonrpc" in data:
                self.write(data)
            elif "id" in msg:  # the core refused the request itself (token, host)
                self.write({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32000, "message": f"{data.get('message')} {data.get('fix')}"}})

    def forward(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.write({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Not valid JSON."}})
            return
        try:
            self._post(line, msg)
        except httpx.TransportError:
            with self._start_lock:
                up = core_is_up(self.client) or start_core(self.client)
            if not up:
                self._unavailable(msg, "The FVS core is not running and could not be "
                                       "started.")
                return
            try:
                self._post(line, msg)
            except httpx.HTTPError as exc:
                self._unavailable(msg, f"The FVS core did not answer ({exc}).")
        except (httpx.HTTPError, ValueError) as exc:
            self._unavailable(msg, f"The FVS core gave an unreadable answer ({exc}).")


def run(stdin: IO[bytes] | None = None, stdout: IO[bytes] | None = None) -> int:
    config = ensure_config()
    token = ensure_token()
    client = httpx.Client(base_url=f"http://{HOST}:{config.port}",
                          headers={"Authorization": f"Bearer {token}"},
                          timeout=httpx.Timeout(120.0, connect=5.0))
    if not core_is_up(client):
        start_core(client)  # a failure is reported per message
    proxy = Proxy(client, stdout or sys.stdout.buffer)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for raw in stdin or sys.stdin.buffer:
            line = raw.strip()
            if line:
                pool.submit(proxy.forward, line)
    client.close()
    return 0
