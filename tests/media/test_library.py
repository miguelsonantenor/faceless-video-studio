"""LIB-1, LIB-2 on real files: scan, hash cache, read-only input, moves, duplicates."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

import fvs.library
from fvs.core import Core
from fvs.errors import ErrorCode, FvsError
from fvs.pathguard import Area
from tests.media.conftest import number_of

pytestmark = pytest.mark.media


def snapshot(folder: Path) -> dict[str, tuple[int, int, str]]:
    out = {}
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(folder))] = (st.st_size, st.st_mtime_ns,
                                               hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def input_dir(core: Core) -> Path:
    return core.guard.roots[Area.INPUT]


def test_scan_registers_every_clip_with_details(lib_core: Core, clips: dict[str, Path]) -> None:
    result = lib_core.library.scan()
    assert len(result.new) == len(clips) and not result.skipped
    by_name = {c["file_name"]: c for c in result.new}
    assert [c["number"] for c in result.new] == list(range(1, len(clips) + 1))
    assert (by_name["landscape.mp4"]["width"], by_name["landscape.mp4"]["height"]) == (1920, 1080)
    assert (by_name["portrait.mp4"]["width"], by_name["portrait.mp4"]["height"]) == (1080, 1920)
    assert (by_name["phone_vfr.mov"]["width"], by_name["phone_vfr.mov"]["height"]) == (720, 1280)
    assert by_name["nosound_8s.mp4"]["has_audio"] is False
    assert by_name["long_3min.mp4"]["duration_s"] == pytest.approx(180, abs=0.2)
    assert by_name["landscape.mp4"]["title"] == "Landscape"
    assert lib_core.activity.recent(1)[0]["summary"].startswith(
        f"Scanned the input folder: registered {len(clips)} new clip(s) (#1 to #{len(clips)})")


def test_rescan_reads_nothing_again(lib_core: Core, monkeypatch: pytest.MonkeyPatch,
                                    clips: dict[str, Path]) -> None:
    lib_core.library.scan()
    calls: list[Path] = []
    real = fvs.library.file_hash
    monkeypatch.setattr(fvs.library, "file_hash", lambda p: calls.append(p) or real(p))
    again = lib_core.library.scan()
    assert again.new == [] and again.known == len(clips)
    assert calls == []  # cached by path, size and modified time


def test_input_folder_is_unchanged_byte_for_byte(lib_core: Core) -> None:
    before = snapshot(input_dir(lib_core))
    lib_core.library.scan()
    lib_core.analysis.inspect("#3", ["scenes", "black_frozen", "loudness"])
    assert snapshot(input_dir(lib_core)) == before


def test_duplicate_content_is_skipped(lib_core: Core) -> None:
    lib_core.library.scan()
    folder = input_dir(lib_core)
    (folder / "copy_of_portrait.mp4").write_bytes((folder / "portrait.mp4").read_bytes())
    result = lib_core.library.scan()
    assert result.new == []
    n = number_of(lib_core, "portrait.mp4")
    assert result.skipped[0]["reason"] == f"same content as clip #{n}"


def test_moved_clip_keeps_its_number(lib_core: Core) -> None:
    lib_core.library.scan()
    n = number_of(lib_core, "portrait.mp4")
    folder = input_dir(lib_core)
    (folder / "portrait.mp4").rename(folder / "tall.mp4")
    result = lib_core.library.scan()
    assert result.moved == [{"number": n, "from": str(folder / "portrait.mp4"),
                             "to": str(folder / "tall.mp4")}]
    assert lib_core.library.get(n)["file_name"] == "tall.mp4"


def test_unreadable_and_other_files(lib_core: Core) -> None:
    folder = input_dir(lib_core)
    (folder / "broken.mp4").write_bytes(b"not a video at all" * 100)
    (folder / "notes.txt").write_text("hello")
    (folder / ".hidden.mp4").write_bytes(b"x")
    result = lib_core.library.scan()
    skipped = {Path(s["file"]).name: s["reason"] for s in result.skipped}
    assert list(skipped) == ["broken.mp4"]
    assert "could not be read as a video" in skipped["broken.mp4"]


def test_subfolders_only_when_asked(lib_core: Core, clips: dict[str, Path]) -> None:
    folder = input_dir(lib_core)
    (folder / "sub").mkdir()
    (folder / "portrait.mp4").rename(folder / "sub" / "portrait.mp4")
    assert len(lib_core.library.scan().new) == len(clips) - 1
    assert [c["file_name"] for c in lib_core.library.scan(subfolders=True).new] == [
        "portrait.mp4"]


def test_scan_outside_input_is_refused(lib_core: Core, tmp_path: Path) -> None:
    with pytest.raises(FvsError) as info:
        lib_core.library.scan(tmp_path)
    assert info.value.code == ErrorCode.PATH_NOT_ALLOWED


@pytest.mark.skipif(sys.platform != "win32", reason="junction test is Windows-only")
def test_clips_behind_a_link_leading_outside_are_not_registered(
        lib_core: Core, tmp_path: Path, clips: dict[str, Path]) -> None:
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mp4").write_bytes(clips["nosound_8s.mp4"].read_bytes())
    _winapi.CreateJunction(str(outside), str(input_dir(lib_core) / "escape"))
    result = lib_core.library.scan(subfolders=True)
    assert all("secret" not in c["file_name"] for c in result.new)


def test_lookup_by_number_hash_sign_and_id(lib_core: Core) -> None:
    lib_core.library.scan()
    clip = lib_core.library.get(2)
    assert lib_core.library.get("#2") == clip == lib_core.library.get(clip["id"])
    with pytest.raises(FvsError) as info:
        lib_core.library.get(99)
    assert info.value.code == ErrorCode.NOT_FOUND and "fvs_library_scan" in info.value.fix


def test_list_pages(lib_core: Core, clips: dict[str, Path]) -> None:
    lib_core.library.scan()
    page = lib_core.library.list(limit=3)
    assert [c["number"] for c in page["items"]] == [1, 2, 3]
    assert page["total"] == len(clips) and page["next_cursor"] == 3
    rest = lib_core.library.list(cursor=page["next_cursor"], limit=20)
    assert rest["items"][0]["number"] == 4 and rest["next_cursor"] is None
