"""NFR-S3, MCP-9, LIB-2: only configured folders, input is read-only, no escapes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from fvs.config import Folders
from fvs.errors import ErrorCode, FvsError
from fvs.pathguard import Area, PathGuard


@pytest.fixture
def guard(tmp_path: Path) -> PathGuard:
    folders = Folders(input=tmp_path / "in", workspace=tmp_path / "ws", output=tmp_path / "out")
    for p in (folders.input, folders.workspace, folders.assets, folders.output):
        p.mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    return PathGuard(folders)


def _refused(guard: PathGuard, path: Path | str, *args, **kw) -> FvsError:
    with pytest.raises(FvsError) as info:
        guard.resolve(path, *args, **kw)
    assert info.value.code == ErrorCode.PATH_NOT_ALLOWED
    return info.value


def make_link(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))  # no admin rights needed
    else:
        os.symlink(target, link)


def test_paths_inside_are_allowed(guard: PathGuard, tmp_path: Path) -> None:
    clip = tmp_path / "in" / "sub" / "a.mp4"
    assert guard.resolve(clip) == clip.resolve()
    assert guard.resolve(tmp_path / "out" / "new.mp4", write=True).name == "new.mp4"


def test_relative_path_uses_first_allowed_area(guard: PathGuard, tmp_path: Path) -> None:
    got = guard.resolve("music/a.mp3", {Area.ASSETS})
    assert got == (tmp_path / "ws" / "assets" / "music" / "a.mp3").resolve()


def test_outside_and_traversal_are_refused(guard: PathGuard, tmp_path: Path) -> None:
    _refused(guard, tmp_path / "outside" / "x.mp4")
    err = _refused(guard, tmp_path / "in" / ".." / "outside" / "x.mp4")
    assert "outside the folders" in err.message
    assert str(tmp_path / "in") in err.fix or "input" in err.fix
    _refused(guard, "../../outside/x.mp4", {Area.OUTPUT})


def test_input_folder_is_read_only(guard: PathGuard, tmp_path: Path) -> None:
    err = _refused(guard, tmp_path / "in" / "a.mp4", write=True)
    assert "read-only" in err.message
    _refused(guard, tmp_path / "in" / "a.mp4", {Area.INPUT}, write=True)


def test_area_restriction(guard: PathGuard, tmp_path: Path) -> None:
    _refused(guard, tmp_path / "out" / "a.mp4", {Area.INPUT})


def test_link_leading_outside_is_refused(guard: PathGuard, tmp_path: Path) -> None:
    link = tmp_path / "in" / "escape"
    make_link(link, tmp_path / "outside")
    _refused(guard, link / "secret.mp4")


def test_link_inside_allowed_folders_is_fine(guard: PathGuard, tmp_path: Path) -> None:
    link = tmp_path / "in" / "to-output"
    make_link(link, tmp_path / "out")
    assert guard.resolve(link / "a.mp4").is_relative_to((tmp_path / "out").resolve())
