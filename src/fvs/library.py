"""The clip library: scan the input folder and register clips (LIB-1, LIB-2).

- Finds .mp4 .mov .m4v .mkv .webm files, optionally in subfolders.
- A clip's identity is a full-file hash, cached by (path, size, modified time), so a
  re-scan does not read unchanged files again.
- Files are only ever opened for reading; nothing in the input folder is written, moved
  or deleted (LIB-2).
- Detecting clips that changed or disappeared and flagging their projects is LIB-3 (P2).
  In P1 a file whose content changed under a known path is reported, not re-registered.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fvs.activity import ActivityLog
from fvs.analyze.probe import probe
from fvs.errors import ErrorCode, FvsError
from fvs.pathguard import Area, PathGuard
from fvs.store import Database
from fvs.timeutil import now_iso

VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm"})
HASH_CHUNK = 4 * 1024 * 1024
PAGE_SIZE = 20


def file_hash(path: Path) -> str:
    """BLAKE2b-256 of the whole file (LIB-1), read in chunks."""
    h = hashlib.blake2b(digest_size=32)
    with path.open("rb") as f:  # read-only (LIB-2)
        while chunk := f.read(HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def title_from_name(name: str) -> str:
    stem = Path(name).stem
    words = re.sub(r"[_\-.]+", " ", stem).strip()
    words = re.sub(r"\s+\d{1,3}$", "", words) or words  # drop "…_04" take numbers
    return words[:1].upper() + words[1:] if words else stem


@dataclass
class ScanResult:
    folder: str
    new: list[dict[str, Any]] = field(default_factory=list)
    known: int = 0
    moved: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.new:
            nums = [c["number"] for c in self.new]
            span = f"#{nums[0]}" if len(nums) == 1 else f"#{nums[0]} to #{nums[-1]}"
            parts.append(f"registered {len(self.new)} new clip(s) ({span})")
        else:
            parts.append("found no new clips")
        if self.known:
            parts.append(f"{self.known} already known")
        if self.moved:
            parts.append(f"{len(self.moved)} moved")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        return "Scanned the input folder: " + ", ".join(parts) + "."


def clip_public(row: Any, projects_using: int = 0) -> dict[str, Any]:
    r = dict(row)
    return {
        "id": r["id"],
        "number": r["number"],
        "title": title_from_name(r["file_name"]),
        "file_name": r["file_name"],
        "path": r["path"],
        "duration_s": r["duration_s"],
        "width": r["width"],
        "height": r["height"],
        "fps": r["fps"],
        "has_audio": bool(r["has_audio"]),
        "state": r["state"],
        "projects_using": projects_using,
    }


class Library:
    def __init__(self, db: Database, guard: PathGuard, activity: ActivityLog) -> None:
        self.db = db
        self.guard = guard
        self.activity = activity

    # ---- scanning ------------------------------------------------------------------

    def _files(self, folder: Path, subfolders: bool) -> list[Path]:
        pattern = folder.rglob("*") if subfolders else folder.glob("*")
        files = [p for p in pattern
                 if p.suffix.lower() in VIDEO_EXTS and p.is_file()
                 and not any(part.startswith(".") for part in p.relative_to(folder).parts)]
        return sorted(files, key=lambda p: str(p).lower())

    def scan(self, folder: str | Path | None = None, *, subfolders: bool = False,
             source: str = "claude") -> ScanResult:
        root = self.guard.roots[Area.INPUT]
        target = self.guard.resolve(folder, {Area.INPUT}) if folder else root
        if not target.is_dir():
            raise FvsError(ErrorCode.NOT_FOUND, f"The folder {str(target)!r} does not exist.",
                           f"Use the input folder ({root}) or one of its subfolders.")
        result = ScanResult(folder=str(target))

        with self.db.connect() as conn:
            by_path = {r["path"]: dict(r) for r in conn.execute("SELECT * FROM clips")}
        by_hash = {r["content_hash"]: r for r in by_path.values()}

        for path in self._files(target, subfolders):
            # Re-check every file: a link inside the input folder may lead outside it.
            try:
                path = self.guard.resolve(path, {Area.INPUT})
            except FvsError:
                result.skipped.append({"file": str(path),
                                       "reason": "it links outside the input folder"})
                continue
            st = path.stat()
            known = by_path.get(str(path))
            if known and known["size_bytes"] == st.st_size and known["mtime_ns"] == st.st_mtime_ns:
                result.known += 1
                continue

            digest = file_hash(path)
            if known is not None:
                if known["content_hash"] == digest:  # touched, not changed
                    self._update_stat(known["id"], st)
                    result.known += 1
                else:
                    result.skipped.append({
                        "file": str(path),
                        "reason": f"its content changed since it was registered as clip "
                                  f"#{known['number']}; changed-clip handling arrives in "
                                  "phase 2 (LIB-3)"})
                continue

            same = by_hash.get(digest)
            if same is not None:
                if not Path(same["path"]).exists():
                    self._move(same["id"], path, st)
                    result.moved.append({"number": same["number"], "from": same["path"],
                                         "to": str(path)})
                    by_path.pop(same["path"], None)
                    same["path"] = str(path)
                    by_path[str(path)] = same
                else:
                    result.skipped.append({"file": str(path),
                                           "reason": f"same content as clip #{same['number']}"})
                continue

            try:
                info = probe(path)
            except FvsError as exc:
                result.skipped.append({"file": str(path), "reason": exc.message})
                continue
            row = self._register(path, st, digest, info)
            by_path[str(path)] = row
            by_hash[digest] = row
            result.new.append(clip_public(row))

        self.activity.add(result.summary(), source=source,  # type: ignore[arg-type]
                          tool="fvs_library_scan" if source == "claude" else None)
        return result

    def _register(self, path: Path, st: Any, digest: str, info: Any) -> dict[str, Any]:
        at = now_iso()
        with self.db.transaction() as conn:
            number = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM clips").fetchone()[0]
            clip_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO clips (id, number, path, file_name, content_hash, size_bytes, "
                "mtime_ns, duration_s, width, height, fps, has_audio, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (clip_id, number, str(path), path.name, digest, st.st_size, st.st_mtime_ns,
                 info.duration_s, info.width, info.height, info.fps, int(info.has_audio),
                 at, at),
            )
            return dict(conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone())

    def _update_stat(self, clip_id: str, st: Any) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE clips SET size_bytes = ?, mtime_ns = ?, updated_at = ? "
                         "WHERE id = ?", (st.st_size, st.st_mtime_ns, now_iso(), clip_id))

    def _move(self, clip_id: str, path: Path, st: Any) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE clips SET path = ?, file_name = ?, size_bytes = ?, mtime_ns = ?, "
                         "state = 'active', updated_at = ? WHERE id = ?",
                         (str(path), path.name, st.st_size, st.st_mtime_ns, now_iso(), clip_id))

    # ---- lookup --------------------------------------------------------------------

    def get_row(self, ref: int | str) -> dict[str, Any]:
        """A clip by number (3, "3", "#3") or ID."""
        text = str(ref).strip().lstrip("#")
        with self.db.connect() as conn:
            if text.isdigit():
                row = conn.execute("SELECT * FROM clips WHERE number = ?", (int(text),)).fetchone()
            else:
                row = conn.execute("SELECT * FROM clips WHERE id = ?", (text,)).fetchone()
        if row is None:
            raise FvsError(ErrorCode.NOT_FOUND, f"There is no clip {ref!r}.",
                           "Call fvs_library_scan to list the clips and their numbers.")
        return dict(row)

    def get(self, ref: int | str) -> dict[str, Any]:
        row = self.get_row(ref)
        return clip_public(row, self._projects_using([row["id"]]).get(row["id"], 0))

    def list(self, cursor: int = 0, limit: int = PAGE_SIZE) -> dict[str, Any]:
        limit = max(1, min(limit, PAGE_SIZE))
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM clips WHERE number > ? ORDER BY number LIMIT ?",
                (cursor, limit + 1))]
            total = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
        more = len(rows) > limit
        rows = rows[:limit]
        using = self._projects_using([r["id"] for r in rows])
        return {
            "items": [clip_public(r, using.get(r["id"], 0)) for r in rows],
            "total": total,
            "next_cursor": rows[-1]["number"] if more else None,
        }

    def _projects_using(self, clip_ids: list[str]) -> dict[str, int]:
        if not clip_ids:
            return {}
        marks = ",".join("?" * len(clip_ids))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT clip_id, COUNT(*) AS n FROM project_clips WHERE clip_id IN ({marks}) "
                "GROUP BY clip_id", clip_ids).fetchall()
        return {r["clip_id"]: r["n"] for r in rows}
