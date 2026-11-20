from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from fvs.config import Config, Folders, save_config
from fvs.core import Core
from tests.media.make_clips import ensure_clips


@pytest.fixture(scope="session")
def clips() -> dict[str, Path]:
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg not installed")
    return ensure_clips()


def link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)  # instant, and the test still cannot change the original
    except OSError:
        shutil.copy2(src, dst)


def open_core(home: Path, root: Path, clips: dict[str, Path]) -> Core:
    folders = Folders(input=root / "input", workspace=root / "workspace", output=root / "output")
    for name, path in clips.items():
        link_or_copy(path, folders.input / name)
    save_config(Config(folders=folders), home)
    return Core.open(home)


@pytest.fixture
def lib_core(home: Path, clips: dict[str, Path], tmp_path: Path) -> Iterator[Core]:
    """A fresh core whose input folder holds the test clips."""
    core = open_core(home, tmp_path / "FVS", clips)
    yield core
    core.jobs.stop(timeout=5)


@pytest.fixture(scope="module")
def scanned_core(clips: dict[str, Path], tmp_path_factory: pytest.TempPathFactory
                 ) -> Iterator[Core]:
    """One core per test module, with every test clip scanned (analysis is cached)."""
    base = tmp_path_factory.mktemp("scanned")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FVS_HOME", str(base / "dot-fvs"))
        mp.setenv("FVS_ROOT", str(base / "FVS"))
        core = open_core(base / "dot-fvs", base / "FVS", clips)
        core.library.scan()
        yield core
        core.jobs.stop(timeout=5)


def number_of(core: Core, file_name: str) -> int:
    with core.db.connect() as conn:
        return conn.execute("SELECT number FROM clips WHERE file_name = ?",
                            (file_name,)).fetchone()[0]
