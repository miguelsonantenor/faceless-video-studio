"""Script markup (SCR-2, SCR-3, SCR-5; DS 5.4).

- One sentence per line. A script written as one line is split at ``.``, ``!``, ``?``.
- ``[pause 0.8]`` adds 0.1–3 s of silence after the text before it (and ends that
  sentence there, since voice is made sentence by sentence).
- ``{1,000|one thousand}`` shows the first form in captions and speaks the second.
- ``*word*`` marks caption emphasis; it may cover several words and pairs.

Each sentence keeps its token *groups*: ``{"caption", "spoken", "emph"}``. A plain word
is one group with the same caption and spoken text. Groups let the voice service map
spoken word timings back to caption words.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from fvs.errors import ErrorCode, FvsError

PAUSE_MIN_S, PAUSE_MAX_S = 0.1, 3.0
_PAUSE = re.compile(r"\[\s*pause\s+(?P<s>\d*\.?\d+)\s*s?\s*\]", re.IGNORECASE)
_TAG = re.compile(r"\[[^\]]*\]")
_TOKEN = re.compile(r"\*(?P<emph>[^*]+)\*"
                    r"|\{(?P<cap>[^{}|]*)\|(?P<spk>[^{}|]*)\}"
                    r"|(?P<word>[^\s*{}]+)")
_INNER = re.compile(r"\{(?P<cap>[^{}|]*)\|(?P<spk>[^{}|]*)\}|(?P<word>[^\s*{}]+)")
# Sentence end: . ! ? … then any closing quotes, brackets or emphasis stars, then space.
_SENTENCE_END = re.compile(r"([.!?…][\"'”’)\]*]*)\s+")

# Scripts not written with spaces between words (SCR-5): Thai, Lao, Myanmar, Khmer,
# Japanese kana and CJK ideographs.
_NO_SPACE_SCRIPTS = re.compile(
    "[฀-໿က-႟ក-៿぀-ヿ㐀-䶿一-鿿"
    "豈-﫿]")

EXAMPLE = ("The volcano makes its own *lightning*.\n"
           "It happens {1,000|one thousand} times a year. [pause 0.6]\n"
           "Nobody knows exactly why.")


def _invalid(message: str, fix: str) -> FvsError:
    return FvsError(ErrorCode.SCRIPT_INVALID, message, fix, example=EXAMPLE)


@dataclass
class Sentence:
    groups: list[dict[str, Any]]
    pause_after_s: float = 0.0

    @property
    def spoken(self) -> str:
        return " ".join(g["spoken"] for g in self.groups if g["spoken"])

    @property
    def caption(self) -> str:
        return " ".join(g["caption"] for g in self.groups if g["caption"])

    @property
    def emphasis(self) -> list[int]:
        """Indexes of emphasised caption words."""
        out, n = [], 0
        for g in self.groups:
            words = g["caption"].split()
            if g["emph"]:
                out.extend(range(n, n + len(words)))
            n += len(words)
        return out

    @property
    def word_count(self) -> int:
        return len(self.spoken.split())


def check_language(text: str) -> None:
    letters = [c for c in text if unicodedata.category(c).startswith("L")]
    foreign = sum(1 for c in letters if _NO_SPACE_SCRIPTS.match(c))
    if letters and foreign / len(letters) > 0.2:
        raise FvsError(
            ErrorCode.LANGUAGE_UNSUPPORTED,
            "This script is in a language written without spaces between words "
            "(such as Chinese, Japanese or Thai). Version 1 supports only languages "
            "written with spaces between words (SCR-5).",
            "Write the voiceover in a language that separates words with spaces, "
            "such as English, Spanish or French.")


def _atoms(text: str, offset: int, emph: bool, pattern: re.Pattern[str],
           line: str) -> list[tuple[int, int, dict[str, Any]]]:
    """(start, end, group) for each token; anything left over is a markup error."""
    out: list[tuple[int, int, dict[str, Any]]] = []
    pos = 0
    for m in pattern.finditer(text):
        gap = text[pos:m.start()]
        if gap.strip():
            raise _stray(gap.strip(), line)
        pos = m.end()
        start, end = offset + m.start(), offset + m.end()
        if m.groupdict().get("emph") is not None:
            inner = _atoms(m.group("emph"), offset + m.start("emph"), True, _INNER, line)
            if not inner:
                raise _invalid(f"Empty emphasis “**” in: {line}",
                               "Put the words to emphasise between the stars: *word*.")
            inner[0] = (start, inner[0][1], inner[0][2])
            inner[-1] = (inner[-1][0], end, inner[-1][2])
            out.extend(inner)
        elif m.group("cap") is not None:
            cap, spk = m.group("cap").strip(), m.group("spk").strip()
            if not cap and not spk:
                raise _invalid(f"Empty pair “{m.group(0)}” in: {line}",
                               "Write {caption text|spoken text}, e.g. {1,000|one thousand}.")
            out.append((start, end, {"caption": cap, "spoken": spk, "emph": emph}))
        else:
            word = m.group("word")
            if "|" in word:
                raise _invalid(f"“|” outside a pair in: {line}",
                               "Put both forms in braces: {caption text|spoken text}.")
            out.append((start, end, {"caption": word, "spoken": word, "emph": emph}))
    if text[pos:].strip():
        raise _stray(text[pos:].strip(), line)
    return out


def _stray(fragment: str, line: str) -> FvsError:
    return _invalid(f"Unbalanced markup “{fragment}” in: {line}",
                    "Close every *emphasis* with a second star and every "
                    "{caption|spoken} pair with a brace.")


def _groups(text: str, line: str) -> list[dict[str, Any]]:
    """Token groups; tokens with no space between them (``*word*,``) are glued."""
    groups: list[dict[str, Any]] = []
    prev_end = -1
    for start, end, g in _atoms(text, 0, False, _TOKEN, line):
        if groups and start == prev_end:
            last = groups[-1]
            last["caption"] += g["caption"]
            last["spoken"] += g["spoken"]
            last["emph"] = last["emph"] or g["emph"]
        else:
            groups.append(dict(g))
        prev_end = end
    return groups


def _split_sentences(text: str) -> list[str]:
    return [p for p in _SENTENCE_END.sub("\\1\n", text).split("\n") if p.strip()]


def parse_script(text: str) -> list[Sentence]:
    """Parse a voiceover script into numbered sentences (SCR-2, SCR-3)."""
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        raise _invalid("The voiceover is empty.", "Write the voiceover, one sentence per line.")
    one_line = len(lines) == 1
    sentences: list[Sentence] = []

    for line in lines:
        pos = 0
        for m in [*_PAUSE.finditer(line), None]:
            chunk = line[pos:m.start()] if m else line[pos:]
            other = _TAG.search(chunk)
            if other:
                tag = other.group(0)
                if re.match(r"\[\s*voice\s*:", tag, re.IGNORECASE):
                    raise _invalid(f"“{tag}” switches voices, which arrives in phase 3 "
                                   "(SCR-7).", "Remove the [voice: …] line; use one voice.")
                raise _invalid(f"Unknown markup “{tag}” in: {line}",
                               "The only bracket markup is [pause 0.8] (0.1–3 seconds).")
            parts = _split_sentences(chunk) if one_line else ([chunk] if chunk.strip() else [])
            for part in parts:
                groups = _groups(part.strip(), line)
                if not any(g["spoken"] for g in groups):
                    raise _invalid(f"Nothing would be spoken in: {line}",
                                   "Give each sentence words to say; {caption|} shows text "
                                   "without speaking it.")
                sentences.append(Sentence(groups=groups))
            if m is not None:
                seconds = float(m.group("s"))
                if not PAUSE_MIN_S <= seconds <= PAUSE_MAX_S:
                    raise _invalid(f"A pause of {seconds:g} s is outside 0.1–3 s: {m.group(0)}",
                                   "Use a pause from 0.1 to 3 seconds, e.g. [pause 0.8].")
                if not sentences:
                    raise _invalid("The script starts with a pause.",
                                   "Put [pause …] after the sentence it should follow.")
                last = sentences[-1]
                last.pause_after_s = round(min(PAUSE_MAX_S, last.pause_after_s + seconds), 3)
                pos = m.end()
    return sentences
