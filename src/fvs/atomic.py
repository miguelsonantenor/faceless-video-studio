"""Write files to a temp name, then rename when complete (NFR-R1).

Temp files carry the ``.fvs-tmp`` suffix so that start-up recovery can delete any
left behind by a crash.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

TMP_SUFFIX = ".fvs-tmp"


@contextmanager
def atomic_path(final: Path) -> Iterator[Path]:
    """Yield a temp path; on success rename it to ``final``, on error delete it."""
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(final.name + TMP_SUFFIX)
    tmp.unlink(missing_ok=True)
    try:
        yield tmp
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_bytes_atomic(final: Path, data: bytes) -> None:
    with atomic_path(final) as tmp:
        tmp.write_bytes(data)


def write_text_atomic(final: Path, text: str) -> None:
    write_bytes_atomic(final, text.encode("utf-8"))


def remove_temp_files(root: Path) -> list[Path]:
    """Delete every ``*.fvs-tmp`` file under ``root``. Returns what was removed."""
    removed: list[Path] = []
    if not root.exists():
        return removed
    for p in root.rglob(f"*{TMP_SUFFIX}"):
        try:
            p.unlink()
            removed.append(p)
        except OSError:
            pass
    return removed
