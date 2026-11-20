"""Running FFmpeg and ffprobe safely (NFR-S4).

Always an argument list, never a shell. Long runs can be cancelled: the caller passes a
``cancelled`` callable, which is polled while the process runs. On Windows no console
window is opened for the child process.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache

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
) -> Result:
    """Run ``ffmpeg``/``ffprobe`` with ``args``. Never raises on a non-zero exit."""
    argv = [tool_path(name), *args]
    start = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - argument list, no shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
    )
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
