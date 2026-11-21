"""Creator-editable settings (BUILD_PLAN section 1, REV-4, DS 10.4).

Stored in the ``settings`` table as one row per dotted key (``video.max_seconds``).
Defaults are seeded on first start; a stored value always wins over a later default.
Every change is validated as a whole (so cross-field rules hold), saved, written to the
activity feed and published as a ``settings_changed`` event.

Folders, the port and the API key are NOT here: they live in ``~/.fvs/config.toml``.
The default voice is NOT here either: it is ``voice_profiles.is_default``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from fvs.activity import ActivityLog, Source
from fvs.errors import ErrorCode, FvsError
from fvs.events import EventBus
from fvs.store import Database
from fvs.timeutil import now_iso

MIN_FRAME_RATE = 24  # TikTok takes 24-60 fps


class _Group(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VideoSettings(_Group):
    target_min_seconds: int = Field(30, ge=5, le=180)
    target_max_seconds: int = Field(45, ge=5, le=180)
    max_seconds: int = Field(60, ge=5, le=600)
    # Output frames per second: 0 = the clips' own rate, at TikTok's 24-60 fps.
    frame_rate: int = Field(0, ge=0, le=60)


class VoiceSettings(_Group):
    daily_budget_chars: int = Field(20_000, ge=0, le=10_000_000)
    budget_reset_time: str = Field("00:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class ExportSettings(_Group):
    destination: Literal["folder", "folder_and_tiktok_draft"] = "folder"


class DefaultsSettings(_Group):
    template: str = Field("storytime_dark", min_length=1, max_length=80)
    inspiration_set: str = Field("", max_length=80)  # "" = none (INS-3)


class RenderSettings(_Group):
    renders_at_once: int = Field(0, ge=0, le=16)  # 0 = automatic


class PerformanceSettings(_Group):
    """Where heavy work runs: the GPU (encoding, speech recognition) or only the CPU."""

    device: Literal["gpu", "cpu"] = "gpu"


class SafeZoneSettings(_Group):
    """Pixels kept clear in a 1080×1920 frame (DS 5.3)."""

    top: int = Field(108, ge=0, le=900)
    bottom: int = Field(320, ge=0, le=900)
    left: int = Field(60, ge=0, le=500)
    right: int = Field(120, ge=0, le=500)


class Settings(_Group):
    video: VideoSettings = Field(default_factory=VideoSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    export: ExportSettings = Field(default_factory=ExportSettings)
    defaults: DefaultsSettings = Field(default_factory=DefaultsSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)
    safe_zone: SafeZoneSettings = Field(default_factory=SafeZoneSettings)
    performance: PerformanceSettings = Field(default_factory=PerformanceSettings)

    @model_validator(mode="after")
    def _cross_field(self) -> Settings:
        v = self.video
        if v.target_min_seconds > v.target_max_seconds:
            raise ValueError(
                "video.target_min_seconds must be at most video.target_max_seconds"
            )
        if v.target_max_seconds > v.max_seconds:
            raise ValueError("video.target_max_seconds must be at most video.max_seconds")
        if 0 < v.frame_rate < MIN_FRAME_RATE:
            raise ValueError(f"video.frame_rate must be 0 (same as the clips) or "
                             f"{MIN_FRAME_RATE}-60")
        if self.export.destination == "folder_and_tiktok_draft":
            raise ValueError(
                "export.destination 'folder_and_tiktok_draft' is not available yet "
                "(TikTok draft upload, EXP-3, comes in phase 5); use 'folder'"
            )
        return self


# Plain-language names for the activity feed.
LABELS: dict[str, str] = {
    "video.target_min_seconds": "the shortest target length (seconds)",
    "video.target_max_seconds": "the longest target length (seconds)",
    "video.max_seconds": "the maximum video length (seconds)",
    "video.frame_rate": "the frame rate (0 = same as the clips)",
    "voice.daily_budget_chars": "the daily voice budget (characters)",
    "voice.budget_reset_time": "the time the budget day starts",
    "export.destination": "where approved videos go",
    "defaults.template": "the default template",
    "defaults.inspiration_set": "the inspiration set Claude follows",
    "render.renders_at_once": "renders at the same time (0 = automatic)",
    "safe_zone.top": "the top safe-zone margin (px)",
    "safe_zone.bottom": "the bottom safe-zone margin (px)",
    "safe_zone.left": "the left safe-zone margin (px)",
    "safe_zone.right": "the right safe-zone margin (px)",
    "performance.device": "where renders and speech recognition run (gpu or cpu)",
}


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{full}."))
        else:
            out[full] = value
    return out


def unflatten(flat: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in flat.items():
        node = out
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


DEFAULTS: dict[str, Any] = flatten(Settings().model_dump())
KEYS: frozenset[str] = frozenset(DEFAULTS)


def _fmt(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value)


def _validation_error(exc: ValidationError) -> FvsError:
    details = []
    for err in exc.errors():
        path = ".".join(str(p) for p in err["loc"]) or "settings"
        msg = err["msg"].removeprefix("Value error, ")
        details.append({"path": path, "problem": msg, "fix": _fix_for(path, err)})
    summary = "; ".join(f"{d['path']}: {d['problem']}" for d in details)
    return FvsError(
        ErrorCode.SETTINGS_INVALID,
        f"Settings not saved: {summary}.",
        "Change the values listed and save again. Nothing was changed.",
        example='{"video.target_min_seconds": 30, "video.target_max_seconds": 45}',
        details=details,
    )


def _limits(path: str) -> tuple[Any, Any]:
    """The (ge, le) bounds declared on a settings field, e.g. ``video.max_seconds``."""
    group, _, name = path.partition(".")
    group_field = Settings.model_fields.get(group)
    if group_field is None or group_field.annotation is None:
        return None, None
    field = getattr(group_field.annotation, "model_fields", {}).get(name)
    if field is None:
        return None, None
    ge = next((m.ge for m in field.metadata if hasattr(m, "ge")), None)
    le = next((m.le for m in field.metadata if hasattr(m, "le")), None)
    return ge, le


def _fix_for(path: str, err: dict[str, Any]) -> str:
    ctx = err.get("ctx") or {}
    ge, le = _limits(path)
    if ("ge" in ctx or "le" in ctx) and ge is not None and le is not None:
        return f"Use a whole number from {ge} to {le}."
    if "ge" in ctx:
        return f"Use a number of at least {ctx['ge']}."
    if "le" in ctx:
        return f"Use a number of at most {ctx['le']}."
    if "pattern" in ctx:
        return "Use 24-hour time as HH:MM, for example 00:00 or 06:30."
    if err["type"] == "extra_forbidden":
        return f"Unknown setting. Valid settings: {', '.join(sorted(KEYS))}."
    return "See the problem above."


class SettingsService:
    def __init__(self, db: Database, activity: ActivityLog, bus: EventBus) -> None:
        self.db = db
        self.activity = activity
        self.bus = bus

    def seed(self) -> None:
        """Insert defaults for keys not stored yet; never overwrite a stored value."""
        at = now_iso()
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO settings (key, value_json, updated_at) VALUES (?, ?, ?)",
                [(k, json.dumps(v), at) for k, v in DEFAULTS.items()],
            )

    def _stored(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT key, value_json FROM settings").fetchall()
        # Keys from older versions that no longer exist are ignored.
        return {r["key"]: json.loads(r["value_json"]) for r in rows if r["key"] in KEYS}

    def get(self) -> Settings:
        return Settings.model_validate(unflatten({**DEFAULTS, **self._stored()}))

    def get_flat(self) -> dict[str, Any]:
        return flatten(self.get().model_dump())

    def update(self, changes: dict[str, Any], *, source: Source = "creator") -> Settings:
        """Apply changes given as dotted keys (``{"video.max_seconds": 90}``) or nested."""
        flat_changes = flatten(changes)
        unknown = sorted(set(flat_changes) - KEYS)
        if unknown:
            raise FvsError(
                ErrorCode.SETTINGS_INVALID,
                f"Unknown setting(s): {', '.join(unknown)}. Nothing was changed.",
                f"Use only these settings: {', '.join(sorted(KEYS))}.",
                example='{"voice.daily_budget_chars": 30000}',
                details=[
                    {"path": k, "problem": "unknown setting", "fix": "Remove it"}
                    for k in unknown
                ],
            )
        before = self.get_flat()
        try:
            new = Settings.model_validate(unflatten({**before, **flat_changes}))
        except ValidationError as exc:
            raise _validation_error(exc) from None
        after = flatten(new.model_dump())
        changed = {k: (before[k], after[k]) for k in flat_changes if before[k] != after[k]}
        if not changed:
            return new

        at = now_iso()
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, "
                "updated_at = excluded.updated_at",
                [(k, json.dumps(new_v), at) for k, (_old, new_v) in changed.items()],
            )
        who = {"creator": "Creator", "claude": "Claude", "engine": "FVS"}[source]
        for key, (old, new_v) in changed.items():
            self.activity.add(
                f"{who} changed {LABELS.get(key, key)} from {_fmt(old)} to {_fmt(new_v)}.",
                source=source,
                tool="fvs_settings" if source == "claude" else None,
            )
        self.bus.publish(
            {"type": "settings_changed", "changed": {k: v[1] for k, v in changed.items()}}
        )
        return new


def effective_renders_at_once(settings: Settings) -> int:
    n = settings.render.renders_at_once
    if n > 0:
        return n
    return max(1, (os.cpu_count() or 2) // 2)
