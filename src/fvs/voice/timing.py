"""From ElevenLabs character timings to caption word timings (DS 5.4 step 5)."""

from __future__ import annotations

from typing import Any


def word_times(alignment: dict[str, list[Any]]) -> list[tuple[str, float, float]]:
    """(word, start, end) for each whitespace-separated word of the spoken text."""
    words: list[tuple[str, float, float]] = []
    current: list[tuple[str, float, float]] = []
    triples = zip(alignment["characters"], alignment["character_start_times_seconds"],
                  alignment["character_end_times_seconds"], strict=False)
    for ch, start, end in [*triples, (" ", 0.0, 0.0)]:
        if ch.isspace():
            if current:
                words.append(("".join(c for c, _, _ in current),
                              float(current[0][1]), float(current[-1][2])))
                current = []
        else:
            current.append((ch, start, end))
    return words


def _spread(out: list[dict[str, Any]], group: dict[str, Any], start: float,
            end: float) -> None:
    """Share a group's time span evenly among its caption words."""
    words = group["caption"].split()
    if not words:
        return
    step = (end - start) / len(words)
    for k, w in enumerate(words):
        out.append({"word": w, "start": round(start + k * step, 3),
                    "end": round(start + (k + 1) * step, 3), "emph": group["emph"]})


def caption_words(groups: list[dict[str, Any]], alignment: dict[str, list[Any]],
                  duration_s: float) -> list[dict[str, Any]]:
    """Caption words with times, relative to the start of the sentence audio.

    Spoken words are matched to the script's token groups in order: a ``{1,000|one
    thousand}`` group spans the times of "one" and "thousand". If ElevenLabs returned a
    different number of words than the script has (it can normalise text), the caption
    words are spread over the speech in proportion to their length instead.
    """
    spoken = word_times(alignment)
    counts = [len(g["spoken"].split()) for g in groups]
    out: list[dict[str, Any]] = []
    if spoken and sum(counts) == len(spoken):
        i = 0
        for g, n in zip(groups, counts, strict=True):
            if n:
                start, end = spoken[i][1], spoken[i + n - 1][2]
                i += n
            else:  # shown but not spoken: no time of its own
                start = end = out[-1]["end"] if out else spoken[0][1]
            _spread(out, g, start, end)
        return out

    first = spoken[0][1] if spoken else 0.0
    last = spoken[-1][2] if spoken else duration_s
    weights = [max(1, len(g["spoken"] or g["caption"])) for g in groups]
    total, t = float(sum(weights)), first
    for g, w in zip(groups, weights, strict=True):
        span = (last - first) * w / total
        _spread(out, g, t, t + span)
        t += span
    return out
