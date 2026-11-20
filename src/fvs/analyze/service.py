"""Clip inspection: inline when quick, a background job when not (ANA-7, MCP-3).

``inspect`` is what ``fvs_clip_inspect`` (WP6) will call. It returns the cached analysis
when everything asked for is there; computes it inline when the estimate is within
``INLINE_LIMIT_S``; otherwise starts (or joins) an ``analyze`` job and returns its ID
together with whatever is already known.
"""

from __future__ import annotations

from typing import Any

from fvs.activity import ActivityLog
from fvs.analyze.analyzer import EXTRAS, Analyzer
from fvs.errors import ErrorCode, FvsError
from fvs.jobs import JobContext, JobQueue
from fvs.library import Library, clip_public

INLINE_LIMIT_S = 15.0  # DS 8.3: a job when analysis would take more than 15 s


def normalise_extras(extras: list[str] | set[str] | None) -> frozenset[str]:
    if extras is None:
        return frozenset(EXTRAS)
    unknown = set(extras) - set(EXTRAS)
    if unknown:
        raise FvsError(
            ErrorCode.SPEC_INVALID,
            f"Unknown extras: {', '.join(sorted(unknown))}.",
            f"Use any of: {', '.join(EXTRAS)}.",
            example='{"clip": 3, "extras": ["scenes", "transcript"]}',
        )
    return frozenset(extras)


def describe(clip: dict[str, Any], result: dict[str, Any]) -> str:
    parts = []
    d = result.get("details") or {}
    if d:
        parts.append(f"{d['duration_s']:.1f} s {d['orientation']}")
    if result.get("scenes") is not None:
        parts.append(f"{len(result['scenes'])} scene(s)")
    bad = len(result.get("black") or []) + len(result.get("frozen") or [])
    if result.get("black") is not None and bad:
        parts.append(f"{bad} black or frozen stretch(es)")
    t = result.get("transcript")
    if isinstance(t, dict):
        if t.get("available"):
            parts.append("speech found" if t.get("has_speech") else "no speech")
        elif not clip.get("has_audio"):
            parts.append("no sound")
    return f"Analysed clip #{clip['number']}: " + ", ".join(parts) + "."


class AnalysisService:
    def __init__(self, library: Library, analyzer: Analyzer, jobs: JobQueue,
                 activity: ActivityLog) -> None:
        self.library = library
        self.analyzer = analyzer
        self.jobs = jobs
        self.activity = activity
        jobs.register("analyze", self.run_job)

    def inspect(self, ref: int | str, extras: list[str] | set[str] | None = None,
                *, inline_limit_s: float = INLINE_LIMIT_S) -> dict[str, Any]:
        clip = self.library.get_row(ref)
        wanted = normalise_extras(extras)
        missing = self.analyzer.missing(clip, wanted)
        if "transcript" in missing and not self.analyzer.whisper.available:
            missing.discard("transcript")  # cannot be computed; result() explains why
        base = {"clip": clip_public(clip)}
        if not missing:
            return {**base, "status": "ready", **self.analyzer.result(clip, wanted)}

        estimate = self.analyzer.estimate_seconds(clip, missing)
        if estimate <= inline_limit_s:
            result = self.analyzer.analyze(clip, wanted)
            return {**base, "status": "ready", **result}

        job = self.jobs.submit(
            "analyze", {"clip_id": clip["id"], "extras": sorted(wanted)},
            idempotency_key=f"analyze:{clip['content_hash']}:{','.join(sorted(wanted))}",
            reuse_done=False,
        )
        partial = self.analyzer.result(clip, wanted)
        return {**base, "status": "analysing", "job_id": job["id"], "estimate_s": estimate,
                **partial,
                "next": "Wait with fvs_job_get, then call fvs_clip_inspect again."}

    def run_job(self, ctx: JobContext) -> dict[str, Any]:
        clip = self.library.get_row(ctx.payload["clip_id"])
        wanted = normalise_extras(ctx.payload.get("extras"))
        result = self.analyzer.analyze(
            clip, wanted, cancelled=lambda: ctx.cancelled,
            progress=lambda f, note: ctx.progress(f, note))
        return {"summary": describe(clip, result), "clip": clip["number"]}
