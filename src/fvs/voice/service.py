"""Voiceovers, sentence by sentence (VO-1–7, DS 5.4, DS 8.5, AC-8).

1. Each sentence's cache key is built from its spoken text, voice, model and settings.
   A cached sentence is reused at no cost (VO-4).
2. Missing sentences are made in order with ElevenLabs' with-timestamps endpoint,
   passing up to 3 previous request IDs for stitching where the model allows it (VO-2).
   Each call is recorded in the cost ledger, and the budget is checked before each one.
3. The sentence audio is decoded to 48 kHz samples, joined with exact silence for
   ``[pause]`` markup (VO-3), and written as one WAV track plus a timings file with
   sentence and caption-word times.

A run that stops halfway (error, budget, cancel) loses nothing: every sentence already
made stays cached and is never billed again.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fvs.activity import ActivityLog, Source
from fvs.atomic import write_bytes_atomic, write_text_atomic
from fvs.errors import ErrorCode, FvsError
from fvs.jobs import JobCancelled, JobContext, JobQueue
from fvs.media.audio import decode_pcm, write_wav
from fvs.projects import Projects, next_state
from fvs.script.service import ScriptService, fit_note, fit_of
from fvs.settings import SettingsService
from fvs.store import Database
from fvs.timeutil import now_iso, utcnow
from fvs.voice.budget import Budget
from fvs.voice.elevenlabs import ElevenLabs
from fvs.voice.profiles import VoiceProfile, VoiceProfiles
from fvs.voice.timing import caption_words

SAMPLE_RATE = 48_000
STITCH_MAX_AGE = timedelta(hours=2)  # ElevenLabs only stitches to recent requests
STITCH_COUNT = 3
QUOTA_KEEP_S = 60.0


def _fresh(created_at: str) -> bool:
    return utcnow() - datetime.fromisoformat(created_at) < STITCH_MAX_AGE


class VoiceService:
    def __init__(self, db: Database, *, projects: Projects, scripts: ScriptService,
                 profiles: VoiceProfiles, client: ElevenLabs, budget: Budget,
                 jobs: JobQueue, settings: SettingsService, activity: ActivityLog,
                 workspace: Path) -> None:
        self.db = db
        self.projects = projects
        self.scripts = scripts
        self.profiles = profiles
        self.client = client
        self.budget = budget
        self.jobs = jobs
        self.settings = settings
        self.activity = activity
        self.cache_dir = workspace / "voice"
        self._quota: tuple[float, dict[str, Any]] | None = None  # (when, status)
        self._quota_lock = threading.Lock()
        jobs.register("voice", self.run_job)

    # ---- planning --------------------------------------------------------------------

    def _plan(self, proj: dict[str, Any], profile: VoiceProfile
              ) -> tuple[list[dict[str, Any]], list[str], dict[str, dict[str, Any]]]:
        sentences = self.scripts.sentences_for_voice(proj)
        keys = [profile.cache_key(s["spoken"]) for s in sentences]
        marks = ",".join("?" * len(keys))
        with self.db.connect() as conn:
            rows = conn.execute(f"SELECT * FROM voice_sentences WHERE cache_key IN ({marks})",
                                keys).fetchall()
        cached = {r["cache_key"]: dict(r) for r in rows
                  if Path(r["audio_path"]).exists() and Path(r["alignment_path"]).exists()}
        return sentences, keys, cached

    def _up_to_date(self, proj: dict[str, Any], keys: list[str]) -> dict[str, Any] | None:
        """The current voiceover, if it was made from exactly these sentences."""
        if not proj["voiceover_id"] or "voiceover" in json.loads(proj["stale_flags"]):
            return None
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM voiceovers WHERE id = ?",
                               (proj["voiceover_id"],)).fetchone()
        if row is None or not Path(row["timings_path"]).exists():
            return None
        timings = json.loads(Path(row["timings_path"]).read_text(encoding="utf-8"))
        return dict(row) | {"timings": timings} if timings["cache_keys"] == keys else None

    def quota_status(self, *, fresh: bool = False) -> dict[str, Any]:
        """The ElevenLabs quota left. The web app shows it on every screen, so the answer
        is kept for QUOTA_KEEP_S; ``fresh`` asks ElevenLabs now (before spending)."""
        with self._quota_lock:
            kept = self._quota
            if not fresh and kept is not None and time.monotonic() - kept[0] < QUOTA_KEEP_S:
                return kept[1]
            try:
                q = self.client.quota()
            except FvsError as exc:
                status = {"available": False, "reason": exc.message, "fix": exc.fix}
            else:
                status = {"available": True, "limit": q.limit, "left": q.left,
                          "resets_at": q.resets_at}
            self._quota = (time.monotonic(), status)
            return status

    @staticmethod
    def _over_budget_error(to_bill: int, budget: dict[str, Any], *, partial: bool) -> FvsError:
        done = (" The sentences made so far are kept and will not be billed again."
                if partial else "")
        return FvsError(
            ErrorCode.BUDGET_EXCEEDED,
            f"This needs about {to_bill:,} characters, but only {budget['left']:,} of "
            f"today's {budget['limit']:,}-character voice budget are left.{done}",
            "Ask the creator whether to go over today's budget; if they agree, call again "
            "with allow_over_budget=true. Otherwise wait until the budget resets at "
            f"{budget['resets_at']} (UTC).")

    # ---- public API ------------------------------------------------------------------

    def generate(self, project: int | str, *, voice: str | None = None,
                 dry_run: bool = False, allow_over_budget: bool = False,
                 source: Source = "claude", step: JobContext | None = None
                 ) -> dict[str, Any]:
        """Make the voiceover: inline when every sentence is cached or when ``step`` (a
        batch step) is given, otherwise as a ``voice`` job."""
        proj = self.projects.get_row(project)
        profile = self.profiles.get(voice)
        sentences, keys, cached = self._plan(proj, profile)
        missing: dict[str, int] = {}
        for s, k in zip(sentences, keys, strict=True):
            if k not in cached:
                missing.setdefault(k, len(s["spoken"]))
        to_bill = sum(missing.values())
        reused = sum(len(s["spoken"]) for s, k in zip(sentences, keys, strict=True)
                     if k in cached)
        existing = self._up_to_date(proj, keys)
        budget = self.budget.status()
        ref = f"#{proj['number']}"

        if dry_run:
            return {
                "dry_run": True, "project": proj["number"], "voice": profile.alias,
                "sentences": len(sentences), "to_make": len(missing),
                "chars_to_bill": 0 if existing else to_bill,
                "chars_reused": reused, "up_to_date": existing is not None,
                "over_budget": not existing and to_bill > budget["left"],
                "budget": budget, "quota": self.quota_status(fresh=True),
                "summary": (f"The voiceover for {ref} is up to date; nothing to bill."
                            if existing else
                            f"{ref}: {len(missing)} of {len(sentences)} sentences to make, "
                            f"about {to_bill:,} characters to bill, {reused:,} reused; "
                            f"{budget['left']:,} left in today's budget."),
            }
        if existing:
            return self._result(proj, existing, existing["timings"], billed=0,
                                reused=existing["chars_billed"] + existing["chars_reused"],
                                unchanged=True)
        next_state(proj, "voiceover_made")
        if to_bill > budget["left"] and not allow_over_budget:
            raise self._over_budget_error(to_bill, budget, partial=False)
        if to_bill > budget["left"]:
            self.activity.add(
                f"Going over today's voice budget for {ref} by about "
                f"{to_bill - budget['left']:,} characters, as the creator agreed.",
                source=source, project_id=proj["id"])
        if not missing:  # everything cached: joining is quick, do it now
            return self._build(proj, profile, allow_over_budget=allow_over_budget,
                               source=source)
        if step is not None:
            return self._build(proj, profile, allow_over_budget=allow_over_budget,
                               source=source, progress=step.progress,
                               cancelled=lambda: step.cancelled)

        job = self.jobs.submit(
            "voice", {"voice": profile.alias, "script_version": proj["script_v"],
                      "allow_over_budget": allow_over_budget, "source": source},
            project_id=proj["id"], reuse_done=False,
            idempotency_key="voice:" + hashlib.sha1("".join(keys).encode()).hexdigest())
        self.activity.add(f"Started the voiceover for {ref}: {len(missing)} sentence(s) to "
                          f"make, about {to_bill:,} characters.", source=source,
                          project_id=proj["id"],
                          tool="fvs_voiceover_generate" if source == "claude" else None)
        return {"status": "generating", "job_id": job["id"], "project": proj["number"],
                "chars_to_bill": to_bill, "chars_reused": reused, "budget": budget,
                "summary": f"Making the voiceover for {ref}; wait with fvs_job_get."}

    def timings(self, proj: dict[str, Any]) -> dict[str, Any]:
        """The current voiceover's sentence and word timings (the edit's timeline)."""
        if not proj["voiceover_id"] or "voiceover" in json.loads(proj["stale_flags"]):
            raise FvsError(ErrorCode.WRONG_STATE,
                           f"Project #{proj['number']} has no voiceover for its current "
                           "script, and the edit is timed to the voiceover.",
                           f"Make it first with fvs_voiceover_generate for #{proj['number']}.")
        with self.db.connect() as conn:
            row = conn.execute("SELECT timings_path FROM voiceovers WHERE id = ?",
                               (proj["voiceover_id"],)).fetchone()
        return json.loads(Path(row["timings_path"]).read_text(encoding="utf-8"))

    def run_job(self, ctx: JobContext) -> dict[str, Any]:
        proj = self.projects.get_row(ctx.project_id or "")
        if proj["script_v"] != ctx.payload["script_version"]:
            raise FvsError(ErrorCode.WRONG_STATE,
                           f"The script of #{proj['number']} changed while the voiceover "
                           "was waiting to start.",
                           "Call fvs_voiceover_generate again for the new script.")
        return self._build(proj, self.profiles.get(ctx.payload["voice"]),
                           allow_over_budget=ctx.payload["allow_over_budget"],
                           source=ctx.payload["source"], progress=ctx.progress,
                           cancelled=lambda: ctx.cancelled)

    # ---- making and joining ----------------------------------------------------------

    def _make(self, proj: dict[str, Any], profile: VoiceProfile, text: str, key: str,
              previous_ids: list[str], allow_over_budget: bool) -> tuple[dict[str, Any], int]:
        budget = self.budget.status()  # read every time: a new limit applies at once
        if len(text) > budget["left"] and not allow_over_budget:
            raise self._over_budget_error(len(text), budget, partial=True)
        speech = self.client.speak(
            voice_id=profile.voice_id, model_id=profile.model_id, text=text,
            voice_settings=profile.voice_settings(),
            previous_request_ids=previous_ids if profile.stitching else None)
        audio_path = self.cache_dir / f"{key}.mp3"
        align_path = self.cache_dir / f"{key}.json"
        write_bytes_atomic(audio_path, speech.audio)
        write_text_atomic(align_path, json.dumps(
            {"alignment": speech.alignment, "request_id": speech.request_id}))
        row = {"cache_key": key, "voice_alias": profile.alias, "model_id": profile.model_id,
               "audio_path": str(audio_path), "alignment_path": str(align_path),
               "duration_s": float(speech.alignment["character_end_times_seconds"][-1]
                                   if speech.alignment["character_end_times_seconds"] else 0),
               "chars_billed": speech.chars_billed, "request_id": speech.request_id,
               "created_at": now_iso()}
        with self.db.transaction() as conn:
            conn.execute(f"INSERT OR REPLACE INTO voice_sentences ({', '.join(row)}) "
                         f"VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
            Budget.record(conn, chars=speech.chars_billed, cache_key=key,
                          project_id=proj["id"],
                          over_budget_approved=speech.chars_billed > budget["left"])
        return row, speech.chars_billed

    def _build(self, proj: dict[str, Any], profile: VoiceProfile, *,
               allow_over_budget: bool, source: Source,
               progress: Callable[[float, str | None], None] | None = None,
               cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        import numpy as np

        sentences, keys, cached = self._plan(proj, profile)
        billed = reused = 0
        rows: list[dict[str, Any]] = []
        for n, (s, key) in enumerate(zip(sentences, keys, strict=True)):
            if cancelled is not None and cancelled():
                raise JobCancelled()
            row = cached.get(key)
            if row is None:
                previous = [r["request_id"] for r in rows[-STITCH_COUNT:]
                            if r["request_id"] and _fresh(r["created_at"])]
                row, chars = self._make(proj, profile, s["spoken"], key, previous,
                                        allow_over_budget)
                cached[key] = row
                billed += chars
            else:
                reused += len(s["spoken"])
            rows.append(row)
            if progress is not None:
                progress(0.9 * (n + 1) / len(sentences),
                         f"Sentence {n + 1} of {len(sentences)}")

        chunks, timed, offset = [], [], 0.0
        for s, row in zip(sentences, rows, strict=True):
            pcm = decode_pcm(Path(row["audio_path"]), SAMPLE_RATE, cancelled)
            length = len(pcm) / SAMPLE_RATE
            alignment = json.loads(Path(row["alignment_path"]).read_text(encoding="utf-8"))
            words = [{**w, "start": round(w["start"] + offset, 3),
                      "end": round(w["end"] + offset, 3)}
                     for w in caption_words(s["groups"], alignment["alignment"], length)]
            timed.append({"i": s["i"], "start": round(offset, 3),
                          "end": round(offset + length, 3), "caption": s["caption"],
                          "spoken": s["spoken"], "pause_after_s": s["pause_after_s"],
                          "words": words})
            pause = int(round(s["pause_after_s"] * SAMPLE_RATE))
            chunks += [pcm, np.zeros(pause, dtype=np.float32)]
            offset += length + pause / SAMPLE_RATE
        duration = round(offset, 3)

        vo_id = uuid.uuid4().hex
        folder = self.projects.folder(proj) / "voice"
        audio_path, timings_path = folder / f"{vo_id}.wav", folder / f"{vo_id}.json"
        write_wav(audio_path, np.concatenate(chunks), SAMPLE_RATE)
        timings = {"voiceover_id": vo_id, "voice": profile.alias,
                   "script_version": proj["script_v"], "duration_s": duration,
                   "sample_rate": SAMPLE_RATE, "cache_keys": keys, "sentences": timed}
        write_text_atomic(timings_path, json.dumps(timings, ensure_ascii=False, indent=1))

        with self.db.transaction() as conn:
            current = self.projects.get_row(proj["id"], conn)
            conn.execute(
                "INSERT INTO voiceovers (id, project_id, script_version, voice_alias, "
                "audio_path, timings_path, duration_s, chars_billed, chars_reused, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (vo_id, proj["id"], proj["script_v"], profile.alias, str(audio_path),
                 str(timings_path), duration, billed, reused, now_iso()))
            self.projects.advance(conn, current, "voiceover_made",
                                  fields={"voiceover_id": vo_id},
                                  stale_add=("render",) if current["render_id"] else (),
                                  stale_remove=("voiceover",))
        row = {"id": vo_id, "voice_alias": profile.alias, "script_version": proj["script_v"],
               "duration_s": duration}
        result = self._result(self.projects.get_row(proj["id"]), row, timings,
                              billed=billed, reused=reused, unchanged=False)
        if progress is None:  # a job writes its own activity line from the summary
            self.activity.add(result["summary"], source=source, project_id=proj["id"],
                              tool="fvs_voiceover_generate" if source == "claude" else None)
        return result

    def _result(self, proj: dict[str, Any], vo: dict[str, Any], timings: dict[str, Any], *,
                billed: int, reused: int, unchanged: bool) -> dict[str, Any]:
        settings = self.settings.get()
        duration = timings["duration_s"]
        fit = fit_of(duration, settings)
        profile_wpm = self.profiles.get(vo["voice_alias"]).words_per_minute
        note = fit_note(duration, settings, profile_wpm)
        ref = f"#{proj['number']}"
        summary = (f"The voiceover for {ref} is already up to date ({duration:.1f} s)."
                   if unchanged else
                   f"Made the voiceover for {ref}: {duration:.1f} s ({fit.replace('_', ' ')});"
                   f" billed {billed:,} characters, reused {reused:,}.")
        return {
            "project": proj["number"], "voiceover_id": vo["id"], "voice": vo["voice_alias"],
            "script_version": vo["script_version"], "duration_s": duration,
            "fit": fit, "warnings": [note] if note else [], "unchanged": unchanged,
            "sentences": [{"i": s["i"], "start": s["start"], "end": s["end"]}
                          for s in timings["sentences"]],
            "chars_billed": billed, "chars_reused": reused,
            "budget": self.budget.status(), "quota": self.quota_status(fresh=True),
            "state": proj["state"], "summary": summary,
        }
