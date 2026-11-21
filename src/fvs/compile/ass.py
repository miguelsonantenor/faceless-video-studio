"""Captions and the hook as one ASS subtitle file (CAP-1–6, DS 5.5 stage 5).

Built from the voiceover's caption word timings. Modes:

- ``word_highlight``: the words of a chunk (up to ``max_words``) stay on screen while
  the word being spoken turns the highlight colour.
- ``chunks``: 1–3 words at a time.
- ``line``: one sentence at a time, wrapped by libass.
- ``off``: no captions (the hook still shows).

Emphasised words (``*word*``) use the emphasis colour; ``overrides`` replace the text of
a caption word without touching the voice (CAP-6). Margins come from the safe zone so
text stays inside it (CAP-3); libass wraps long lines.
"""

from __future__ import annotations

from typing import Any

from fvs.settings import SafeZoneSettings

CHUNK_LINGER_S = 0.4  # how long the last chunk of a sentence stays after its last word


def colour(hex_rgb: str, alpha: int = 0) -> str:
    """``#RRGGBB`` → ASS ``&HAABBGGRR``."""
    h = hex_rgb.lstrip("#")
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def ts(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def clean(text: str) -> str:
    """Caption text cannot open ASS override blocks or line-break codes."""
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def _chunks(words: list[dict[str, Any]], max_words: int) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    for w in words:
        last = out[-1] if out else None
        if (last is None or len(last) >= max_words or last[-1]["sentence"] != w["sentence"]
                or last[-1]["word"].endswith((".", "!", "?", ",", ";", ":", "…"))):
            out.append([w])
        else:
            last.append(w)
    return out


def build_ass(*, timings: dict[str, Any], captions: Any, style: dict[str, Any], font: str,
              hook_text: str | None, hook_seconds: float, end_s: float,
              safe: SafeZoneSettings) -> str:
    words = [{**w, "sentence": s["i"]} for s in timings["sentences"] for w in s["words"]]
    for o in captions.overrides:
        if o.word <= len(words):
            words[o.word - 1] = {**words[o.word - 1], "word": o.text}
    caps = style.get("all_caps", False)
    base, hi, emph = (colour(style["color"]), colour(style["highlight_color"]),
                      colour(style["emphasis_color"]))

    def word_text(w: dict[str, Any], active: bool = False) -> str:
        text = clean(w["word"].upper() if caps else w["word"])
        if active:
            return f"{{\\c{hi}}}{text}{{\\c{base}}}"
        if w.get("emph"):
            return f"{{\\c{emph}}}{text}{{\\c{base}}}"
        return text

    # A style with hook_box draws the hook as white text on a solid banner (BorderStyle 3,
    # the box in OutlineColour, padded by Outline); otherwise it is outlined text.
    box = style.get("hook_box")
    hook_look = (f"{colour(box)},{colour(box)},-1,0,0,0,100,100,0,0,3,14,0" if box else
                 f"{colour(style['outline_color'])},&H80000000,-1,0,0,0,100,100,0,0,1,"
                 f"{style['outline'] + 1},{style['shadow']}")
    align, margin_v = {"lower_third": (2, safe.bottom + 100), "center": (5, 0),
                       "upper_third": (8, safe.top + 260)}[captions.position]
    bold = -1 if style.get("weight", 400) >= 600 else 0
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: cap,{font},{style['size']},{base},{hi},{colour(style['outline_color'])},"
        f"&H80000000,{bold},0,0,0,100,100,0,0,1,{style['outline']},{style['shadow']},"
        f"{align},{safe.left},{safe.right},{margin_v},1",
        f"Style: hook,{font},{int(style['size'] * 1.05)},{base},{hi},{hook_look},8,"
        f"{safe.left},{safe.right},{safe.top + 120},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    def event(start: float, end: float, text: str, name: str = "cap") -> None:
        if end - start >= 0.01 and text.strip():
            lines.append(f"Dialogue: 0,{ts(start)},{ts(min(end, end_s))},{name},,0,0,0,,{text}")

    if hook_text:
        hook = clean(hook_text.upper() if caps else hook_text)
        event(0.0, min(hook_seconds, end_s), hook, "hook")

    if captions.mode == "line":
        for s in timings["sentences"]:
            ws = [w for w in words if w["sentence"] == s["i"]]
            if ws:
                event(ws[0]["start"], ws[-1]["end"] + CHUNK_LINGER_S,
                      " ".join(word_text(w) for w in ws))
    elif captions.mode in ("chunks", "word_highlight"):
        chunks = _chunks(words, captions.max_words)
        for k, chunk in enumerate(chunks):
            nxt = chunks[k + 1][0]["start"] if k + 1 < len(chunks) else end_s
            same_sentence = k + 1 < len(chunks) and \
                chunks[k + 1][0]["sentence"] == chunk[0]["sentence"]
            stop = nxt if same_sentence else min(nxt, chunk[-1]["end"] + CHUNK_LINGER_S)
            if captions.mode == "chunks":
                event(chunk[0]["start"], stop, " ".join(word_text(w) for w in chunk))
                continue
            for j, w in enumerate(chunk):
                w_end = chunk[j + 1]["start"] if j + 1 < len(chunk) else stop
                event(w["start"] if j else chunk[0]["start"], w_end,
                      " ".join(word_text(x, x is w) for x in chunk))
    return "\n".join(lines) + "\n"
