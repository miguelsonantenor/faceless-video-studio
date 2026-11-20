"""Running FFmpeg and ffprobe safely (NFR-S4).

Always an argument list, never a shell. Long runs can be cancelled: the caller passes a
``cancelled`` callable, which is polled while the process runs. On Windows no console
window is opened for the child process.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from fvs.errors import ErrorCode, FvsError
from fvs.jobs import JobCancelled

POLL_S = 0.2
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


@dataclass
class Result:
    args: list[str]
    returncode: int
    stdout: bytes
    stderr: str

    def stderr_tail(self, lines: int = 20) -> str:
        return "\n".join(self.stderr.strip().splitlines()[-lines:])


@cache
def tool_path(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise FvsError(
            ErrorCode.MEDIA_TOOL_MISSING,
            f"{name} was not found on the PATH.",
            "Install FFmpeg 7 or newer with ffprobe, put its bin folder on the PATH, "
            "and run `fvs doctor`.",
        )
    return found


def run_tool(
    name: str,
    args: Sequence[str],
    *,
    timeout: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    cwd: Path | None = None,
    on_line: Callable[[str], None] | None = None,
) -> Result:
    """Run ``ffmpeg``/``ffprobe`` with ``args``. Never raises on a non-zero exit.

    With ``on_line``, stdout is read as text while the tool runs and each line is passed
    on (FFmpeg's ``-progress pipe:1``); otherwise stdout is collected as bytes.
    """
    argv = [tool_path(name), *args]
    start = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - argument list, no shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
        cwd=cwd,
    )
    if on_line is not None:
        return _run_streaming(proc, argv, on_line, timeout, cancelled, start)
    while True:
        try:
            out, err = proc.communicate(timeout=POLL_S)
            break
        except subprocess.TimeoutExpired:
            if cancelled is not None and cancelled():
                proc.kill()
                proc.communicate()
                raise JobCancelled() from None
            if timeout is not None and time.monotonic() - start > timeout:
                proc.kill()
                out, err = proc.communicate()
                return Result(argv, -9, out, err.decode("utf-8", "replace")
                              + f"\n[fvs] stopped after {timeout:.0f} s")
    return Result(argv, proc.returncode, out, err.decode("utf-8", "replace"))


def _run_streaming(proc: subprocess.Popen[bytes], argv: list[str],
                   on_line: Callable[[str], None], timeout: float | None,
                   cancelled: Callable[[], bool] | None, start: float) -> Result:
    err_chunks: list[bytes] = []

    def read_out() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            on_line(raw.decode("utf-8", "replace").strip())

    def read_err() -> None:
        assert proc.stderr is not None
        err_chunks.extend(proc.stderr)

    readers = [threading.Thread(target=f, daemon=True) for f in (read_out, read_err)]
    for t in readers:
        t.start()
    note = ""
    while proc.poll() is None:
        time.sleep(POLL_S)
        if cancelled is not None and cancelled():
            proc.kill()
            proc.wait()
            raise JobCancelled()
        if timeout is not None and time.monotonic() - start > timeout:
            proc.kill()
            proc.wait()
            note = f"\n[fvs] stopped after {timeout:.0f} s"
            break
    for t in readers:
        t.join(5)
    err = b"".join(err_chunks).decode("utf-8", "replace") + note
    return Result(argv, proc.returncode if not note else -9, b"", err)
