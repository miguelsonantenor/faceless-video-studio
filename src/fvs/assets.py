"""Assets: music, fonts, logos, images and sound effects (AUD-4, NFR-L1).

An asset is imported from an allowed folder into the workspace assets folder, with a name,
tags and a license. No license, no asset (``ASSET_UNLICENSED``): the license is recorded
so every export can say where its music came from.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from fvs.activity import ActivityLog, Source
from fvs.errors import ErrorCode, FvsError
from fvs.media.tools import run_tool
from fvs.pathguard import Area, PathGuard
from fvs.store import Database
from fvs.timeutil import now_iso

PAGE = 20
AUDIO = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
PICTURE = (".png", ".jpg", ".jpeg", ".webp")
KINDS = {"music": ("music", AUDIO), "sfx": ("sfx", AUDIO), "font": ("fonts", (".ttf", ".otf")),
         "logo": ("logos", PICTURE), "image": ("images", PICTURE)}


def audio_seconds(path: Path) -> float | None:
    res = run_tool("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of",
                               "default=noprint_wrappers=1:nokey=1", str(path)], timeout=30)
    try:
        return round(float(res.stdout.decode().strip()), 3)
    except ValueError:
        return None


def asset_public(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "kind": row["kind"], "name": row["name"],
            "duration_s": row["duration_s"], "tags": json.loads(row["tags"]),
            "license": row["license"], "source_url": row["source_url"]}


class AssetService:
    def __init__(self, db: Database, guard: PathGuard, activity: ActivityLog,
                 assets_dir: Path) -> None:
        self.db = db
        self.guard = guard
        self.activity = activity
        self.assets_dir = assets_dir

    def import_file(self, path: str, *, kind: str, license: str, name: str | None = None,
                    tags: list[str] | None = None, source_url: str | None = None,
                    source: Source = "claude") -> dict[str, Any]:
        if kind not in KINDS:
            raise FvsError(ErrorCode.SPEC_INVALID, f"Unknown asset kind {kind!r}.",
                           f"Use one of: {', '.join(KINDS)}.")
        if not license.strip():
            raise FvsError(ErrorCode.ASSET_UNLICENSED,
                           "An asset needs its license recorded (for example \"CC0\", "
                           "\"Epidemic Sound subscription\" or \"made by me\").",
                           "Ask the creator where the file comes from and its license, then "
                           "import it with license set.",
                           example='{"path": "music/calm.mp3", "kind": "music", '
                                   '"license": "CC0", "tags": ["calm"]}')
        src = self.guard.resolve(path, {Area.INPUT, Area.ASSETS})
        folder, exts = KINDS[kind]
        if not src.is_file():
            raise FvsError(ErrorCode.NOT_FOUND, f"There is no file at {src}.",
                           "Put the file in the input or assets folder and pass its path.")
        if src.suffix.lower() not in exts:
            raise FvsError(ErrorCode.SPEC_INVALID, f"A {kind} asset must be one of "
                           f"{', '.join(exts)}, not {src.suffix or 'a file without one'}.",
                           "Convert the file or pick another kind.")
        name = (name or src.stem).strip()
        with self.db.connect() as conn:
            taken = conn.execute("SELECT 1 FROM assets WHERE kind = ? AND name = ?",
                                 (kind, name)).fetchone()
        if taken:
            raise FvsError(ErrorCode.SPEC_INVALID, f"There is already a {kind} asset "
                           f"called {name!r}.", "Pick another name.")
        dest = self.assets_dir / folder / src.name
        if dest.resolve() != src:
            n = 2
            while dest.exists():
                dest = dest.with_name(f"{src.stem}-{n}{src.suffix}")
                n += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)  # the input folder is only read (LIB-2)
        duration = audio_seconds(dest) if kind in ("music", "sfx") else None
        row = {"id": uuid.uuid4().hex, "kind": kind, "name": name, "path": str(dest),
               "duration_s": duration, "tags": json.dumps(sorted(set(tags or []))),
               "license": license.strip(), "source_url": source_url,
               "created_at": now_iso()}
        with self.db.connect() as conn:
            conn.execute(f"INSERT INTO assets ({', '.join(row)}) VALUES "
                         f"({', '.join('?' * len(row))})", tuple(row.values()))
        summary = f"Imported the {kind} “{name}” ({license.strip()})."
        self.activity.add(summary, source=source,
                          tool="fvs_asset_import" if source == "claude" else None)
        return {"asset": asset_public(row), "summary": summary}

    def list_assets(self, *, kind: str | None = None, tag: str | None = None,
                    cursor: int | None = None) -> dict[str, Any]:
        where, args = ["rowid > ?"], [cursor or 0]
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if tag:
            where.append("EXISTS (SELECT 1 FROM json_each(assets.tags) WHERE value = ?)")
            args.append(tag)
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                f"SELECT rowid AS rn, * FROM assets WHERE {' AND '.join(where)} "
                "ORDER BY rowid LIMIT ?", (*args, PAGE + 1))]
        more = len(rows) > PAGE
        items = [asset_public(r) for r in rows[:PAGE]]
        what = f"{kind} " if kind else ""
        return {"items": items, "next_cursor": rows[PAGE - 1]["rn"] if more else None,
                "summary": f"{len(items)} {what}asset(s)" + (f" tagged {tag}" if tag else "")
                           + (" (more with next_cursor)" if more else "") + "."}
