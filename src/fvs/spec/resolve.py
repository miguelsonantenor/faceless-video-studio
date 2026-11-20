"""Cue resolution and validation (DS 5.3, EDT-2, EDT-3, EDT-5, AC-6).

The voiceover's sentence timings define the output timeline:

1. Each sentence owns a slot from its start to the next sentence's start (pauses belong
   to the sentence before them). The last slot ends at the last word plus the tail.
2. ``sentences [a, b]`` covers slots a..b; ``words [f, t]`` runs from word f's start to
   the start of word t+1 (a word that begins a sentence starts at its slot start, so
   word and sentence cues line up); ``seconds d`` runs d seconds from the previous
   segment's end; ``until "end"`` runs to the end of the timeline.
3. Segments follow each other with no gap or overlap and cover the whole timeline. A
   fade of s seconds starts s before the segment's start, overlapping the previous one.
4. A segment's source runs ``in`` to ``in + length × speed``. If that passes the clip's
   end, ``fit`` decides: error, slow (≥ 0.8×), hold_last or loop.
5. Every boundary is snapped to the 30 fps frame grid.
6. Rules: hook ≤ 8 words, caption style and music asset exist, override words exist,
   cover inside the video, total within ``output.max_seconds``.

Every problem comes back with its JSON path, what is wrong and how to fix it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fvs.errors import ErrorCode, FvsError
from fvs.spec.models import EditSpec

FPS = 30
HALF_FRAME = 0.5 / FPS
MIN_SPEED = 0.8
HOOK_MAX_WORDS = 8


def snap(t: float) -> float:
    return round(round(t * FPS) / FPS, 4)


@dataclass
class Context:
    timings: dict[str, Any]  # the voiceover timings file (WP2)
    clips: dict[int, dict[str, Any]]  # the project's clips by number
    caption_styles: set[str]
    assets: dict[str, dict[str, Any]] = field(default_factory=dict)  # music by name and ID
    script_hook: str | None = None

    def clip_number(self, ref: int | str) -> int | None:
        text = str(ref).strip().lstrip("#")
        if text.isdigit():
            return int(text) if int(text) in self.clips else None
        return next((n for n, c in self.clips.items() if c["id"] == text), None)


def _span(a: int, b: int, word: str) -> str:
    return f"{word} {a}" if a == b else f"{word}s {a}–{b}"


class _Resolver:
    def __init__(self, spec: EditSpec, ctx: Context) -> None:
        self.spec, self.ctx = spec, ctx
        self.errors: list[dict[str, str]] = []
        self.shortfalls = 0
        self.warnings: list[str] = []
        sents = ctx.timings["sentences"]
        self.sentences = sents
        self.words: list[dict[str, Any]] = []
        self.word_sentence: list[int] = []
        self.first_word: dict[int, int] = {}
        for si, s in enumerate(sents):
            for w in s["words"]:
                self.first_word.setdefault(si, len(self.words) + 1)
                self.words.append(w)
                self.word_sentence.append(si)
        last_end = self.words[-1]["end"] if self.words else sents[-1]["end"]
        self.end = snap(last_end + spec.end.tail_seconds)
        starts = [s["start"] for s in sents]
        self.slots = [(starts[k], starts[k + 1] if k + 1 < len(sents) else self.end)
                      for k in range(len(sents))]

    def error(self, path: str, problem: str, fix: str) -> None:
        self.errors.append({"path": path, "problem": problem, "fix": fix})

    def _word_start(self, g: int) -> float:
        si = self.word_sentence[g - 1]
        return self.slots[si][0] if self.first_word[si] == g else self.words[g - 1]["start"]

    def _cue_bounds(self, k: int, seg: Any, cursor: float
                    ) -> tuple[float, float, str] | None:
        cue, path = seg.cue, f"timeline.segments[{k}].cue"
        n, nw = len(self.sentences), len(self.words)
        if cue.sentences is not None:
            a, b = cue.sentences[0], cue.sentences[-1]
            if b > n:
                self.error(path, f"sentence {b} does not exist; the voiceover has {n}.",
                           f"Use sentence numbers from 1 to {n}.")
                return None
            return self.slots[a - 1][0], self.slots[b - 1][1], _span(a, b, "sentence")
        if cue.words is not None:
            f, t = cue.words
            if t > nw:
                self.error(path, f"word {t} does not exist; the voiceover has {nw} words.",
                           f"Use word numbers from 1 to {nw} (see fvs_project_get).")
                return None
            end = self.end if t == nw else self._word_start(t + 1)
            return self._word_start(f), end, _span(f, t, "word")
        if cue.seconds is not None:
            return cursor, cursor + cue.seconds, f"{cue.seconds:g} s"
        return cursor, self.end, "the rest of the video"

    def _fit(self, k: int, seg: Any, number: int, length: float, what: str
             ) -> tuple[float, float, float, bool]:
        """(src_out, speed, hold_s, loop) for a segment of ``length`` output seconds."""
        clip = self.ctx.clips[number]
        duration = float(clip["duration_s"] or 0)
        start = seg.in_
        path = f"timeline.segments[{k}]"
        if start >= duration:
            self.error(f"{path}.in", f"in {start:g} s is at or past the end of clip "
                       f"#{number} ({duration:.1f} s).",
                       f"Use an in below {duration:.1f}.")
            return start, 1.0, 0.0, False
        have = duration - start
        if length <= have + 1e-6:
            return round(start + length, 4), 1.0, 0.0, False
        if seg.fit == "slow" and have / length >= MIN_SPEED:
            return duration, round(have / length, 4), 0.0, False
        if seg.fit == "hold_last":
            return duration, 1.0, round(length - have, 4), False
        if seg.fit == "loop":
            return duration, 1.0, 0.0, True
        options = []
        if duration >= length:
            options.append(f"use an earlier in (≤ {duration - length:.1f})")
        if have / length >= MIN_SPEED:
            options.append('set fit "slow"')
        options += ['set fit "hold_last" or "loop"', "cue less", "or use another clip"]
        self.shortfalls += 1
        verb = "need" if what.startswith(("sentences", "words")) else "needs"
        problem = (f"clip #{number} has {have:.1f} s left after {start:g} s but {what} "
                   f"{verb} {length:.1f} s.")
        if seg.fit == "slow":
            problem += f" Slowing to {have / length:.2f}× would be below the {MIN_SPEED}× limit."
        self.error(path, problem, ("; ".join(options) + ".").capitalize())
        return duration, 1.0, 0.0, False

    def run(self) -> dict[str, Any]:
        spec, out, cursor, prev_len = self.spec, [], 0.0, 0.0
        for k, seg in enumerate(spec.timeline.segments):
            path = f"timeline.segments[{k}]"
            number = self.ctx.clip_number(seg.clip)
            bounds = self._cue_bounds(k, seg, cursor)
            if number is None:
                allowed = ", ".join(f"#{n}" for n in sorted(self.ctx.clips))
                self.error(f"{path}.clip", f"clip {seg.clip!r} is not one of this project's "
                           f"clips ({allowed}).", f"Use one of {allowed}.")
            if bounds is None or number is None:
                cursor = snap(bounds[1]) if bounds else cursor
                continue
            start, end, what = snap(bounds[0]), snap(bounds[1]), bounds[2]
            if abs(start - cursor) > HALF_FRAME:
                gap = "a gap" if start > cursor else "an overlap"
                self.error(f"{path}.cue", f"this segment starts at {start:.2f} s but the "
                           f"previous one ends at {cursor:.2f} s ({gap}).",
                           "Cue segments in order so each starts where the last ended, "
                           "e.g. sentences [1], [2, 3], [4] …")
            if end > self.end + HALF_FRAME:
                self.error(f"{path}.cue", f"{what} runs to {end:.2f} s, past the end of the "
                           f"video at {self.end:.2f} s.", 'Shorten it or use {"until": "end"}.')
                end = self.end
            if end - start < 1 / FPS:
                self.error(f"{path}.cue", "this segment covers no time.",
                           "Remove it or give it a cue with some length.")
                cursor = max(cursor, end)
                continue
            fade = seg.transition_in.seconds if seg.transition_in.type == "fade" else 0.0
            fade = snap(fade)
            if fade and k == 0:
                self.error(f"{path}.transition_in", "the first segment cannot fade in.",
                           'Use "cut" for the first segment.')
                fade = 0.0
            elif fade and fade >= min(prev_len, end - start):
                self.error(f"{path}.transition_in", f"a {fade:g} s fade is longer than one of "
                           "the two segments it joins.", "Use a shorter fade or a cut.")
                fade = 0.0
            video_start = snap(start - fade)
            length = round(end - video_start, 4)
            src_out, speed, hold, loop = self._fit(k, seg, number, length, what)
            if end - start < 0.4:
                self.warnings.append(f"{path} lasts only {end - start:.2f} s.")
            spoken = [s for s, (a, b) in zip(self.sentences, self.slots, strict=True)
                      if a < end - HALF_FRAME and b > start + HALF_FRAME]
            out.append({
                "index": k, "clip": number, "clip_id": self.ctx.clips[number]["id"],
                "out_start": start, "out_end": end, "video_start": video_start,
                "src_in": seg.in_, "src_out": src_out, "speed": speed, "hold_s": hold,
                "loop": loop, "motion": seg.motion,
                "transition_in": {"type": "fade" if fade else "cut", "seconds": fade},
                "sentences": [s["i"] for s in spoken],
                "text": " ".join(s["spoken"] for s in spoken),
            })
            cursor, prev_len = end, end - start
        if cursor < self.end - HALF_FRAME and not self.errors:
            self.error("timeline.segments", f"the segments stop at {cursor:.2f} s but the "
                       f"voiceover needs the video to run to {self.end:.2f} s.",
                       'End the last segment with {"until": "end"}, or add segments.')
        self._rules()
        return {"duration_s": self.end, "fps": FPS,
                "voiceover_id": self.ctx.timings.get("voiceover_id"),
                "segments": out, "warnings": self.warnings}

    def _rules(self) -> None:
        spec, ctx = self.spec, self.ctx
        if self.end > spec.output.max_seconds:
            self.error("output.max_seconds", f"the video would last {self.end:.1f} s, more "
                       f"than the maximum of {spec.output.max_seconds:g} s.",
                       "Shorten the script, or raise the maximum in Settings.")
        if spec.captions.style not in ctx.caption_styles:
            styles = ", ".join(sorted(ctx.caption_styles))
            self.error("captions.style", f"there is no caption style "
                       f"{spec.captions.style!r}.", f"Use one of: {styles}.")
        for j, o in enumerate(spec.captions.overrides):
            if o.word > len(self.words):
                self.error(f"captions.overrides[{j}].word", f"word {o.word} does not exist; "
                           f"the voiceover has {len(self.words)} caption words.",
                           f"Use a word number from 1 to {len(self.words)}.")
        hook = spec.hook.text if spec.hook.text is not None else ctx.script_hook
        if hook and len(hook.split()) > HOOK_MAX_WORDS:
            self.error("hook.text", f"the hook has {len(hook.split())} words; the limit "
                       f"is {HOOK_MAX_WORDS}.", "Shorten the hook to 8 words or fewer.")
        if spec.hook.seconds > self.end:
            self.error("hook.seconds", "the hook would show longer than the video.",
                       f"Use at most {self.end:.1f} s.")
        if spec.cover.at_seconds >= self.end:
            self.error("cover.at_seconds", f"{spec.cover.at_seconds:g} s is past the end of "
                       f"the video ({self.end:.1f} s).", f"Pick a time below {self.end:.1f}.")
        music = spec.audio.music
        if music is not None:
            asset = ctx.assets.get(music.asset)
            if asset is None:
                self.error("audio.music.asset", f"there is no music asset {music.asset!r}.",
                           "List music with fvs_assets_list, or import a track with "
                           "fvs_asset_import.")
            elif asset["duration_s"] and music.start_at >= asset["duration_s"]:
                self.error("audio.music.start_at", f"start_at {music.start_at:g} s is past "
                           f"the end of {music.asset} ({asset['duration_s']:.1f} s).",
                           f"Use a start_at below {asset['duration_s']:.1f}.")


def resolve(spec: EditSpec, ctx: Context) -> dict[str, Any]:
    """The resolved timeline, or an FvsError listing every problem."""
    r = _Resolver(spec, ctx)
    timeline = r.run()
    if r.errors:
        only_short = r.shortfalls == len(r.errors)
        first = r.errors[0]
        raise FvsError(
            ErrorCode.CUE_UNRESOLVED if only_short else ErrorCode.SPEC_INVALID,
            f"The Edit Spec has {len(r.errors)} problem(s); first, {first['path']}: "
            f"{first['problem']}",
            "Fix each problem in details (each has a path and a fix). Nothing was saved.",
            details=r.errors)
    return timeline
