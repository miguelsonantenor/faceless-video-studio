"""Run the eight checks on a finished render and make its contact sheet (QA-1–3)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fvs.analyze.detect import detect
from fvs.analyze.probe import probe
from fvs.analyze.sheet import make_sheet
from fvs.compile.compiler import Inputs
from fvs.qa import checks as qa
from fvs.see.listen import compare
from fvs.see.transcripts import Transcripts, heard_words
from fvs.spec.models import EditSpec


def run_qa(*, final: Path, folder: Path, ass_name: str | None, ass_text: str | None,
           spec: EditSpec, timeline: dict[str, Any], inputs: Inputs, fonts_dir: Path,
           transcripts: Transcripts, sentences: list[str],
           cancelled: Callable[[], bool] | None = None,
           progress: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    def step(frac: float, note: str) -> None:
        if progress is not None:
            progress(frac, note)

    info = probe(final)
    step(0.1, "Checking picture and sound")
    det = detect(final, duration_s=info.duration_s, start_s=info.start_s,
                 has_audio=info.has_audio, cancelled=cancelled)
    loud = det.loudness or {}
    last_word = max((w["end"] for s in inputs.timings["sentences"] for w in s["words"]),
                    default=0.0)
    checks = [
        qa.check_format(info, inputs.fps),
        qa.check_tail(info.duration_s, last_word),
        qa.check_loudness(loud.get("integrated_lufs"), spec.audio.loudness.target_lufs),
        qa.check_peak(loud.get("true_peak_dbtp"), spec.audio.loudness.true_peak_db),
    ]

    step(0.4, "Listening to the render")
    if transcripts.whisper.available:
        heard = heard_words(transcripts.get(final, cancelled=cancelled,
                                            duration_s=info.duration_s))
        checks.append(qa.check_voice(compare(sentences, heard)))
    else:
        checks.append(qa.check_voice(None, "The Whisper model is not installed; run "
                                           "`fvs whisper-download` once to check the voice."))

    step(0.8, "Checking captions")
    events = qa.caption_events(ass_text) if ass_text else []
    boxes = qa.caption_boxes(folder, ass_name, inputs.fonts_dir, qa.sample_times(events),
                             info.duration_s, cancelled) if ass_name and events else []
    checks.append(qa.check_safe_zone(boxes, inputs.safe, captions_on=bool(events)))
    checks.append(qa.check_still(det.black, det.frozen, timeline))
    checks.append(qa.check_glyphs("".join(e["text"] for e in events),
                                  qa.font_file(fonts_dir, inputs.font), inputs.font))

    sheet = final.with_suffix(".jpg")
    make_sheet(final, sheet, width=info.width, height=info.height,
               duration_s=info.duration_s, cancelled=cancelled)
    status = qa.overall(checks)
    bad = [c for c in checks if c.status != "pass"]
    summary = ("All 8 checks passed." if not bad else
               f"QA {status}: " + "; ".join(f"{c.name} {c.status} ({c.measured})" for c in bad))
    return {"status": status, "checks": [c.to_dict() for c in checks],
            "contact_sheet": str(sheet), "summary": summary}
