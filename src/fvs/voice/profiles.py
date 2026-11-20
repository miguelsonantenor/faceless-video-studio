"""Named voice profiles (VO-5): voice ID, model and voice settings, one the default."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fvs.activity import ActivityLog, Source
from fvs.errors import ErrorCode, FvsError
from fvs.store import Database

# Models that accept previous_request_ids for stitching (VO-2); Eleven v3 does not.
NO_STITCHING_MODELS = frozenset({"eleven_v3"})


class VoiceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    voice_id: str = Field(min_length=1, max_length=64)
    model_id: str = Field("eleven_multilingual_v2", min_length=1, max_length=64)
    stability: float = Field(0.5, ge=0, le=1)
    similarity: float = Field(0.75, ge=0, le=1)
    style: float = Field(0.0, ge=0, le=1)
    speed: float = Field(1.0, ge=0.7, le=1.2)
    language: str = Field("en", min_length=2, max_length=8)
    words_per_minute: int = Field(150, ge=80, le=260)  # for length estimates (SCR-4)

    def voice_settings(self) -> dict[str, Any]:
        """The ``voice_settings`` body of an ElevenLabs request."""
        return {"stability": self.stability, "similarity_boost": self.similarity,
                "style": self.style, "speed": self.speed, "use_speaker_boost": True}

    @property
    def stitching(self) -> bool:
        return self.model_id not in NO_STITCHING_MODELS

    def cache_key(self, spoken_text: str) -> str:
        """VO-4: the same text, voice, model and settings is never paid for twice."""
        blob = json.dumps([spoken_text, self.voice_id, self.model_id, self.voice_settings()],
                          sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# George, a premade ElevenLabs narrator voice available on every account.
DEFAULT_PROFILE = VoiceProfile(alias="narrator", voice_id="JBFqnCBsd6RMkjVDRZzb")
_SETTINGS = ("stability", "similarity", "style", "speed", "words_per_minute")


def _from_row(row: Any) -> VoiceProfile:
    return VoiceProfile(alias=row["alias"], voice_id=row["voice_id"],
                        model_id=row["model_id"], language=row["language"],
                        **json.loads(row["settings_json"]))


class VoiceProfiles:
    def __init__(self, db: Database, activity: ActivityLog) -> None:
        self.db = db
        self.activity = activity

    def seed(self) -> None:
        """A first start gets one default profile, so voice works once a key is saved."""
        with self.db.transaction() as conn:
            if conn.execute("SELECT COUNT(*) FROM voice_profiles").fetchone()[0] == 0:
                self._upsert(conn, DEFAULT_PROFILE, default=True)

    def list(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM voice_profiles ORDER BY alias").fetchall()
        return [{**_from_row(r).model_dump(), "is_default": bool(r["is_default"])}
                for r in rows]

    def get(self, alias: str | None = None) -> VoiceProfile:
        """A profile by alias, or the default one."""
        with self.db.connect() as conn:
            if alias:
                row = conn.execute("SELECT * FROM voice_profiles WHERE alias = ?",
                                   (alias,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM voice_profiles WHERE is_default = 1"
                                   ).fetchone()
        if row is None:
            names = ", ".join(p["alias"] for p in self.list()) or "none yet"
            raise FvsError(ErrorCode.NOT_FOUND,
                           f"There is no voice profile {alias!r}." if alias
                           else "No voice profile is set as the default.",
                           f"Use one of the voice profiles ({names}), or create one in "
                           "Settings → Voices.")
        return _from_row(row)

    def save(self, data: dict[str, Any], *, make_default: bool = False,
             source: Source = "creator") -> VoiceProfile:
        try:
            profile = VoiceProfile.model_validate(data)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                                 for e in exc.errors())
            raise FvsError(ErrorCode.SETTINGS_INVALID,
                           f"Voice profile not saved: {problems}.",
                           "Fix the fields listed and save again.",
                           example='{"alias": "narrator", "voice_id": "JBFqnCBsd6RMkjVDRZzb"}'
                           ) from None
        with self.db.transaction() as conn:
            self._upsert(conn, profile, default=make_default)
        self.activity.add(f"Saved the voice profile “{profile.alias}”"
                          + (" as the default." if make_default else "."), source=source)
        return profile

    @staticmethod
    def _upsert(conn: Any, p: VoiceProfile, *, default: bool) -> None:
        if default:
            conn.execute("UPDATE voice_profiles SET is_default = 0 WHERE is_default = 1")
        conn.execute(
            "INSERT INTO voice_profiles (alias, voice_id, model_id, settings_json, language, "
            "is_default) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (alias) DO UPDATE SET "
            "voice_id = excluded.voice_id, model_id = excluded.model_id, "
            "settings_json = excluded.settings_json, language = excluded.language, "
            "is_default = MAX(voice_profiles.is_default, excluded.is_default)",
            (p.alias, p.voice_id, p.model_id,
             json.dumps({k: getattr(p, k) for k in _SETTINGS}), p.language, int(default)))
