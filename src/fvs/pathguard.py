"""The path allow-list (NFR-S3, MCP-9, LIB-2).

Every path that reaches FVS from outside (a tool argument, a web request) goes through
``PathGuard.resolve``. The path is fully resolved, following symlinks and Windows
junctions, and must land inside one of the configured folders. The input folder is
read-only: asking for a writable path there is refused.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from fvs.config import Folders
from fvs.errors import ErrorCode, FvsError


class Area(StrEnum):
    INPUT = "input"
    WORKSPACE = "workspace"
    ASSETS = "assets"
    OUTPUT = "output"


READ_ONLY_AREAS = frozenset({Area.INPUT})


class PathGuard:
    def __init__(self, folders: Folders) -> None:
        f = folders.expanded()
        # Resolve the roots too, so a root that is itself a link compares correctly.
        self.roots: dict[Area, Path] = {
            Area.INPUT: f.input.resolve(),
            Area.WORKSPACE: f.workspace.resolve(),
            Area.ASSETS: f.assets.resolve(),
            Area.OUTPUT: f.output.resolve(),
        }

    def resolve(
        self,
        path: str | Path,
        areas: set[Area] | frozenset[Area] | None = None,
        *,
        write: bool = False,
    ) -> Path:
        """Return the resolved path if it is allowed, else raise ``PATH_NOT_ALLOWED``.

        ``areas`` limits which folders are acceptable (default: all). Relative paths are
        taken relative to the first allowed area.
        """
        allowed = set(areas) if areas else set(self.roots)
        if write:
            allowed -= READ_ONLY_AREAS
        if not allowed:
            raise self._refuse(path, "no writable folder matches this request", allowed)

        raw = Path(path).expanduser()
        if not raw.is_absolute():
            first = next(a for a in Area if a in allowed)
            raw = self.roots[first] / raw
        try:
            # strict=False: the file may not exist yet (an output we are about to write).
            resolved = raw.resolve(strict=False)
        except (OSError, RuntimeError) as exc:  # loops, bad names
            raise self._refuse(path, f"it cannot be resolved ({exc})", allowed) from exc

        for area in allowed:
            if resolved.is_relative_to(self.roots[area]):
                return resolved

        if write and resolved.is_relative_to(self.roots[Area.INPUT]):
            raise self._refuse(path, "the input folder is read-only; FVS never writes there",
                               allowed)
        raise self._refuse(path, "it is outside the folders FVS may use", allowed)

    def _refuse(self, path: str | Path, why: str, allowed: set[Area]) -> FvsError:
        folders = ", ".join(f"{a.value} ({self.roots[a]})" for a in Area if a in allowed)
        return FvsError(
            ErrorCode.PATH_NOT_ALLOWED,
            f"The path {str(path)!r} is not allowed: {why}.",
            f"Use a path inside one of these folders: {folders or 'none'}.",
        )
