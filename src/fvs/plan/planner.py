"""The planner: propose a full Edit Spec (EDT-1, EDT-5). Nothing is saved.

1. Cut the voiceover into pieces: one per sentence slot, and a long sentence into
   word-range pieces no longer than the template's ``max_shot_seconds``. The last piece
   runs ``until: end`` so it also covers the tail.
2. Turn each clip's analysis into usable shots: its scenes, minus black and frozen
   stretches, at least 0.5 s long. A clip without analysis is analysed now when that is
   quick, otherwise used as one shot.
3. Give each piece a shot long enough for it, least-used first and not from the clip
   just used, so every scene is used once before any is used again. A used part of a
   scene is not used twice. When nothing is long enough, slow a nearly long enough shot
   (≥ 0.8×), cross a scene cut within the clip, or loop or hold, and say so in a note.
4. Apply the template (reframe, audio, captions, hook, end, motion and transitions),
   then the caller's changes, and validate the result like a save would.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from fvs.analyze.analyzer import Analyzer
from fvs.errors import FvsError
from fvs.inspiration import Inspiration
from fvs.projects import Projects
from fvs.settings import SettingsService
from fvs.spec.models import End
from fvs.spec.resolve import FPS, MIN_SPEED, snap
from fvs.spec.service import SpecService, merge_patch
from fvs.spec.templates import Templates
from fvs.voice.service import VoiceService

MIN_SHOT_S = 0.5
INLINE_ANALYSIS_S = 15.0
IN_OFFSET_S = 0.2  # start a little after a cut, so the shot does not open on a transition


def _subtract(start: float, end: float, bad: list[dict[str, float]]) -> list[list[float]]:
    pieces = [[start, end]]
    for b in bad:
        nxt = []
        for a, z in pieces:
            if b["end"] <= a or b["start"] >= z:
                nxt.append([a, z])
                continue
            if b["start"] > a:
                nxt.append([a, b["start"]])
            if b["end"] < z:
                nxt.append([b["end"], z])
        pieces = nxt
    return [p for p in pieces if p[1] - p[0] >= MIN_SHOT_S]


class Planner:
    def __init__(self, *, projects: Projects, voice: VoiceService, specs: SpecService,
                 templates: Templates, analyzer: Analyzer, settings: SettingsService,
                 inspiration: Inspiration | None = None) -> None:
        self.projects = projects
        self.voice = voice
        self.specs = specs
        self.templates = templates
        self.analyzer = analyzer
        self.settings = settings
        self.inspiration = inspiration

    # ---- shots -----------------------------------------------------------------------

    def _shots(self, clip: dict[str, Any], notes: list[str]) -> list[dict[str, Any]]:
        extras = {"scenes", "black_frozen"}
        found = self.analyzer.result(clip, extras)
        if found["scenes"] is None and Path(clip["path"]).exists() and \
                self.analyzer.estimate_seconds(clip, extras) <= INLINE_ANALYSIS_S:
            found = self.analyzer.analyze(clip, extras)
        duration = float(clip["duration_s"] or 0)
        scenes = found["scenes"]
        if scenes is None:
            notes.append(f"Clip #{clip['number']} is not analysed yet, so it was used as one "
                         "shot; run fvs_clip_inspect on it for scene-aware choices.")
            scenes = [{"start": 0.0, "end": duration}]
        bad = (found.get("black") or []) + (found.get("frozen") or [])
        return [{"clip": clip["number"], "start": a, "end": z, "used": 0, "order": i}
                for i, sc in enumerate(scenes)
                for a, z in _subtract(sc["start"], sc["end"], bad)]

    # ---- pieces of the voiceover -----------------------------------------------------

    @staticmethod
    def _pieces(timings: dict[str, Any], tail: float, max_shot: float
                ) -> list[dict[str, Any]]:
        sents = timings["sentences"]
        words = [w for s in sents for w in s["words"]]
        end = snap((words[-1]["end"] if words else sents[-1]["end"]) + tail)
        pieces: list[dict[str, Any]] = []
        first_word = 1
        for k, s in enumerate(sents):
            a = s["start"]
            b = sents[k + 1]["start"] if k + 1 < len(sents) else end
            n_words = len(s["words"])
            parts = min(math.ceil((b - a) / max_shot), max(1, n_words // 2))
            if parts <= 1:
                pieces.append({"cue": {"sentences": [s["i"]]}, "start": a, "end": b,
                               "label": f"sentence {s['i']}"})
            else:
                cuts, last = [], 0
                for j in range(1, parts):
                    if last + 1 >= n_words:
                        break
                    target = a + j * (b - a) / parts
                    idx = min(range(last + 1, n_words),
                              key=lambda m: abs(s["words"][m]["start"] - target))
                    if idx > last:
                        cuts.append(idx)
                        last = idx
                bounds = [0, *cuts, n_words]
                for lo, hi in zip(bounds, bounds[1:], strict=False):
                    p_start = a if lo == 0 else s["words"][lo]["start"]
                    p_end = b if hi == n_words else s["words"][hi]["start"]
                    pieces.append({"cue": {"words": [first_word + lo, first_word + hi - 1]},
                                   "start": p_start, "end": p_end,
                                   "label": f"sentence {s['i']} (part)"})
            first_word += n_words
        pieces[-1]["cue"] = {"until": "end"}
        pieces[-1]["end"] = end
        return pieces

    # ---- choosing shots --------------------------------------------------------------

    def _assign(self, piece: dict[str, Any], need: float, shots: list[dict[str, Any]],
                clips: dict[int, dict[str, Any]], prev_clip: int | None,
                notes: list[str]) -> dict[str, Any]:
        def rank(s: dict[str, Any]) -> tuple[int, int, int, int]:
            return (s["used"], s["clip"] == prev_clip, s["order"], s["clip"])

        fitting = [s for s in shots if s["end"] - s["start"] >= need]
        if fitting:
            shot = min(fitting, key=rank)
            start = shot["start"] + min(IN_OFFSET_S, (shot["end"] - shot["start"] - need) / 2)
            shot["used"] += 1
            shot["start"] = start + need  # this part of the scene is used up
            return {"clip": shot["clip"], "in": round(start, 3), "fit": "error"}

        longest = max(shots, key=lambda s: (s["end"] - s["start"], -s["used"]))
        have = longest["end"] - longest["start"]
        longest["used"] += 1
        what = piece["label"]
        if have >= MIN_SPEED * need:
            notes.append(f"No shot is long enough for {what} ({need:.1f} s); clip "
                         f"#{longest['clip']} is slowed to {have / need:.2f}×.")
            return {"clip": longest["clip"], "in": round(longest["start"], 3), "fit": "slow"}
        clip = clips[longest["clip"]]
        duration = float(clip["duration_s"] or 0)
        if duration >= need:
            start = max(0.0, min(longest["start"], duration - need))
            notes.append(f"No single scene is long enough for {what} ({need:.1f} s); the "
                         f"shot from clip #{clip['number']} crosses a scene cut.")
            return {"clip": clip["number"], "in": round(start, 3), "fit": "error"}
        fit = "loop" if duration >= 2.0 else "hold_last"
        notes.append(f"Clip #{clip['number']} is {need - duration:.1f} s short for {what}; "
                     f"it is set to {fit.replace('_', ' ')}. A longer clip would look better.")
        return {"clip": clip["number"], "in": 0.0, "fit": fit}

    # ---- the proposal ----------------------------------------------------------------

    def propose(self, project: int | str, *, template: str | None = None,
                changes: dict[str, Any] | None = None) -> dict[str, Any]:
        proj = self.projects.get_row(project)
        timings = self.voice.timings(proj)
        settings = self.settings.get()
        tpl = self.templates.get(template or proj["template"] or settings.defaults.template)
        plan, base = tpl["plan"], tpl["spec"]
        notes: list[str] = []

        clips = {c["number"]: c for c in self.projects.clips(proj["id"])}
        shots = [s for c in clips.values() for s in self._shots(c, notes)]
        if not shots:
            shots = [{"clip": n, "start": 0.0, "end": float(c["duration_s"] or 0), "used": 0,
                      "order": 0} for n, c in clips.items()]
        tail = base.get("end", {}).get("tail_seconds", End().tail_seconds)
        max_shot = plan["max_shot_seconds"]
        profile = (self.inspiration.cached_profile(settings.defaults.inspiration_set)
                   if self.inspiration else None)
        if profile and profile.get("target_shot_s"):  # INS-3: pace like the examples
            max_shot = profile["target_shot_s"]
            notes.append(f"Shots are up to {max_shot:.1f} s, the median shot of the "
                         f"inspiration set {profile['set']!r}.")
        pieces = self._pieces(timings, tail, max_shot)

        transition = plan["transition"]
        fade = (transition.get("seconds", 0.3)
                if isinstance(transition, dict) and transition.get("type") == "fade" else 0.0)
        segments, prev = [], None
        for k, piece in enumerate(pieces):
            this_fade = fade if k > 0 and min(piece["end"] - piece["start"],
                                              pieces[k - 1]["end"] - pieces[k - 1]["start"]
                                              ) > fade + 1 / FPS else 0.0
            need = snap(piece["end"]) - snap(piece["start"]) + this_fade + 1 / FPS
            choice = self._assign(piece, need, shots, clips, prev, notes)
            seg = {"clip": choice["clip"], "in": choice["in"], "cue": piece["cue"],
                   "motion": plan["motion"][k % len(plan["motion"])], "fit": choice["fit"]}
            if this_fade:
                seg["transition_in"] = {"type": "fade", "seconds": this_fade}
            segments.append(seg)
            prev = choice["clip"]

        reframe = dict(base.get("reframe", {}))
        portrait = [c for c in clips.values() if (c["height"] or 0) > 1.2 * (c["width"] or 1)]
        if len(portrait) * 2 > len(clips) and reframe.get("mode") != "center_crop":
            reframe["mode"] = "center_crop"
            notes.append("Most clips are vertical, so they fill the frame with a centre crop.")
        silent = not any(c["has_audio"] for c in clips.values())
        audio = merge_patch(base.get("audio", {}),
                            {"original": {"mode": "mute"}} if silent else {})

        duration = snap(pieces[-1]["end"])
        spec = {"schema_version": 1, "project": proj["number"], "template": tpl["ref"],
                "output": {"max_seconds": settings.video.max_seconds},
                "timeline": {"segments": segments}, **base, "reframe": reframe,
                "audio": audio, "cover": {"at_seconds": snap(min(1.2, duration / 3))}}
        if changes:
            spec = merge_patch(spec, changes)
        try:
            parsed, timeline, _, _ = self.specs.check(proj, spec)
            spec, problems = parsed.dump(), []
        except FvsError as exc:
            timeline, problems = None, exc.details or [exc.to_dict()]
        summary = (f"Proposed an edit for #{proj['number']} with {tpl['ref']}: "
                   f"{len(segments)} shots from {len({s['clip'] for s in segments})} clip(s), "
                   f"{duration:.1f} s." + (" It needs fixes before saving." if problems else ""))
        return {"project": proj["number"], "template": tpl["ref"], "spec": spec,
                "timeline": timeline, "notes": notes, "problems": problems,
                "summary": summary}
