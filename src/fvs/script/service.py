"""Script versions (SCR-1–5, PRJ-5).

Each save is a new, never-changed version with its numbered sentences. Saving moves the
project to Scripted and marks an existing voiceover and render stale (DS 7). Saving the
same script again is a no-op, so a retried call never creates a duplicate version.
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from fvs.activity import ActivityLog, Source
from fvs.errors import ErrorCode, FvsError
from fvs.projects import Projects, next_state
from fvs.script.markup import Sentence, check_language, parse_script
from fvs.settings import SettingsService
from fvs.store import Database
from fvs.timeutil import now_iso
from fvs.voice.profiles import VoiceProfiles

HOOK_MAX_WORDS = 8
_FIELDS = ("title", "voiceover_text", "hook", "post_caption", "hashtags", "language", "notes")


def estimate_seconds(sentences: list[Sentence], words_per_minute: int) -> float:
    words = sum(s.word_count for s in sentences)
    pauses = sum(s.pause_after_s for s in sentences)
    return round(words * 60.0 / words_per_minute + pauses, 1)


def fit_of(seconds: float, settings: Any) -> str:
    v = settings.video
    if seconds < v.target_min_seconds:
        return "too_short"
    return "too_long" if seconds > v.target_max_seconds else "fits"


def fit_note(seconds: float, settings: Any, words_per_minute: int) -> str | None:
    """A plain sentence saying how far from the target length the script is."""
    v = settings.video
    target = f"the target is {v.target_min_seconds}–{v.target_max_seconds} s"
    if seconds < v.target_min_seconds:
        add = math.ceil((v.target_min_seconds - seconds) * words_per_minute / 60)
        return f"About {seconds:.0f} s; {target}. Add about {add} words."
    if seconds > v.target_max_seconds:
        cut = math.ceil((seconds - v.target_max_seconds) * words_per_minute / 60)
        return f"About {seconds:.0f} s; {target}. Cut about {cut} words."
    return None


def _hashtags(tags: list[str] | str | None) -> list[str]:
    if tags is None:
        return []
    items = tags.replace(",", " ").split() if isinstance(tags, str) else tags
    out = []
    for t in items:
        t = "#" + str(t).strip().lstrip("#").replace(" ", "")
        if len(t) > 1 and t not in out:
            out.append(t)
    return out


def sentence_public(i: int, s: dict[str, Any]) -> dict[str, Any]:
    return {"i": i, "caption": s["caption_text"], "spoken": s["spoken_text"],
            "pause_after_s": s["pause_after_s"], "emphasis": json.loads(s["emphasis"])}


class ScriptService:
    def __init__(self, db: Database, projects: Projects, profiles: VoiceProfiles,
                 settings: SettingsService, activity: ActivityLog) -> None:
        self.db = db
        self.projects = projects
        self.profiles = profiles
        self.settings = settings
        self.activity = activity

    def save(self, project: int | str, *, voiceover: str, title: str | None = None,
             hook: str | None = None, post_caption: str | None = None,
             hashtags: list[str] | str | None = None, language: str | None = None,
             notes: str | None = None, voice: str | None = None,
             source: Source = "claude") -> dict[str, Any]:
        proj = self.projects.get_row(project)
        next_state(proj, "script_saved")  # refuse early, before any parsing work
        check_language(voiceover)
        sentences = parse_script(voiceover)
        hook = (hook or "").strip() or None
        if hook and len(hook.split()) > HOOK_MAX_WORDS:
            raise FvsError(ErrorCode.SCRIPT_INVALID,
                           f"The hook has {len(hook.split())} words; it may have at most "
                           f"{HOOK_MAX_WORDS} (SCR-1).",
                           "Shorten the hook to 8 words or fewer.",
                           example='"hook": "This volcano makes its own lightning"')
        profile = self.profiles.get(voice)
        settings = self.settings.get()
        est = estimate_seconds(sentences, profile.words_per_minute)
        if est > settings.video.max_seconds:
            cut = math.ceil((est - settings.video.max_seconds) * profile.words_per_minute / 60)
            raise FvsError(ErrorCode.SCRIPT_TOO_LONG,
                           f"The voiceover would last about {est:.0f} s, longer than the "
                           f"maximum of {settings.video.max_seconds} s. Nothing was saved.",
                           f"Cut about {cut} words, then save again.")

        previous = self.current(proj) if proj["script_v"] else None
        fields = {
            "title": (title or (previous or {}).get("title") or proj["title"]).strip(),
            "voiceover_text": voiceover.strip(),
            "hook": hook,
            "post_caption": (post_caption or "").strip() or None,
            "hashtags": json.dumps(_hashtags(hashtags)),
            "language": language or (previous or {}).get("language") or profile.language,
            "notes": (notes or "").strip() or None,
        }
        with self.db.transaction() as conn:
            proj = self.projects.get_row(proj["id"], conn)
            current = None
            if proj["script_v"]:
                current = conn.execute(
                    "SELECT * FROM script_versions WHERE project_id = ? AND version = ?",
                    (proj["id"], proj["script_v"])).fetchone()
            if current is not None and all(current[k] == fields[k] for k in _FIELDS):
                unchanged, version = True, proj["script_v"]
            else:
                unchanged = False
                version = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM script_versions "
                    "WHERE project_id = ?", (proj["id"],)).fetchone()[0]
                sv_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO script_versions (id, project_id, version, title, "
                    "voiceover_text, hook, post_caption, hashtags, language, notes, "
                    "est_seconds, author, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?)",
                    (sv_id, proj["id"], version, *(fields[k] for k in _FIELDS), est,
                     "creator" if source == "creator" else "claude", now_iso()))
                conn.executemany(
                    "INSERT INTO sentences (script_version_id, i, spoken_text, caption_text, "
                    "pause_after_s, emphasis, tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(sv_id, i, s.spoken, s.caption, s.pause_after_s, json.dumps(s.emphasis),
                      json.dumps(s.groups, ensure_ascii=False))
                     for i, s in enumerate(sentences, 1)])
                stale = tuple(part for part, col in (("voiceover", "voiceover_id"),
                                                     ("render", "render_id")) if proj[col])
                self.projects.advance(conn, proj, "script_saved",
                                      fields={"script_v": version}, stale_add=stale)

        fit = fit_of(est, settings)
        note = fit_note(est, settings, profile.words_per_minute)
        warnings = [note] if note else []
        if not hook:
            warnings.append("No hook: add one of 8 words or fewer to show in the first "
                            "seconds (CAP-4).")
        summary = (f"Script v{version} for #{proj['number']} is unchanged."
                   if unchanged else
                   f"Saved script v{version} for #{proj['number']} “{fields['title']}”: "
                   f"{len(sentences)} sentences, about {est:.0f} s"
                   f" ({fit.replace('_', ' ')}).")
        if not unchanged:
            self.activity.add(summary, source=source, project_id=proj["id"],
                              tool="fvs_script_save" if source == "claude" else None)
        script = self.current(self.projects.get_row(proj["id"]))
        return {**script, "unchanged": unchanged, "fit": fit,
                "target": {"min_seconds": settings.video.target_min_seconds,
                           "max_seconds": settings.video.target_max_seconds,
                           "hard_max_seconds": settings.video.max_seconds},
                "warnings": warnings, "summary": summary}

    def current(self, proj: dict[str, Any]) -> dict[str, Any]:
        """The project's current script version with its sentences."""
        if not proj["script_v"]:
            raise FvsError(ErrorCode.NOT_FOUND,
                           f"Project #{proj['number']} has no script yet.",
                           "Save one with fvs_script_save.")
        with self.db.connect() as conn:
            row = dict(conn.execute(
                "SELECT * FROM script_versions WHERE project_id = ? AND version = ?",
                (proj["id"], proj["script_v"])).fetchone())
            sents = conn.execute("SELECT * FROM sentences WHERE script_version_id = ? "
                                 "ORDER BY i", (row["id"],)).fetchall()
        return {
            "project": proj["number"], "version": row["version"], "title": row["title"],
            "voiceover": row["voiceover_text"], "hook": row["hook"],
            "post_caption": row["post_caption"], "hashtags": json.loads(row["hashtags"]),
            "language": row["language"], "notes": row["notes"], "author": row["author"],
            "est_seconds": row["est_seconds"],
            "sentences": [sentence_public(s["i"], dict(s)) for s in sents],
        }

    def sentences_for_voice(self, proj: dict[str, Any]) -> list[dict[str, Any]]:
        """The current version's sentences with their token groups, for the voice."""
        self.current(proj)  # NOT_FOUND when there is no script
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT s.* FROM sentences s JOIN script_versions v "
                "ON v.id = s.script_version_id WHERE v.project_id = ? AND v.version = ? "
                "ORDER BY s.i", (proj["id"], proj["script_v"])).fetchall()
        return [{"i": r["i"], "spoken": r["spoken_text"], "caption": r["caption_text"],
                 "pause_after_s": r["pause_after_s"], "groups": json.loads(r["tokens"])}
                for r in rows]
