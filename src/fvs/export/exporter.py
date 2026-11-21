"""The export package written when the creator approves a video (EXP-1, CAP-7).

``output/YYYY-MM-DD/YYYY-MM-DD_NN_slug/`` holds:
- ``video.mp4``: a copy of the reviewed render (never a second render);
- ``caption.txt``: the post caption and hashtags;
- ``cover.jpg``: the frame at ``cover.at_seconds``;
- ``captions.srt``: one cue per sentence, with caption overrides applied;
- ``project.json``: the script, Edit Spec, render and QA that made the video.

The folder is built under a temp name and renamed when complete (NFR-R1). Approving the
same project again after a reopen writes a new folder with a number on the end.
"""

from __future__ import annotations

import json
import os
import shutil
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

from fvs import __version__
from fvs.analyze.sheet import grab_frame
from fvs.atomic import TMP_SUFFIX
from fvs.projects import Projects, project_public
from fvs.script.service import ScriptService
from fvs.spec.models import Override
from fvs.spec.service import SpecService
from fvs.voice.service import VoiceService

SRT_LINE = 42


def srt_time(t: float) -> str:
    ms = max(0, round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(timings: dict[str, Any], overrides: list[Override]) -> str:
    words = [{**w, "sentence": s["i"]} for s in timings["sentences"] for w in s["words"]]
    for o in overrides:
        if o.word <= len(words):
            words[o.word - 1] = {**words[o.word - 1], "word": o.text}
    cues = []
    for s in timings["sentences"]:
        ws = [w for w in words if w["sentence"] == s["i"]]
        if not ws:
            continue
        text = "\n".join(textwrap.wrap(" ".join(w["word"] for w in ws), SRT_LINE))
        cues.append(f"{len(cues) + 1}\n{srt_time(ws[0]['start'])} --> "
                    f"{srt_time(ws[-1]['end'])}\n{text}\n")
    return "\n".join(cues)


def caption_text(post_caption: str | None, hashtags: list[str]) -> str:
    tags = " ".join("#" + h.lstrip("#") for h in hashtags if h.strip("# "))
    return "\n\n".join(p for p in ((post_caption or "").strip(), tags) if p) + "\n"


class Exporter:
    def __init__(self, *, projects: Projects, scripts: ScriptService, specs: SpecService,
                 voice: VoiceService, output: Path) -> None:
        self.projects = projects
        self.scripts = scripts
        self.specs = specs
        self.voice = voice
        self.output = output

    def _folder(self, proj: dict[str, Any]) -> Path:
        day = datetime.now().astimezone().strftime("%Y-%m-%d")
        base = self.output / day / f"{day}_{proj['number']:02d}_{proj['slug']}"
        folder, n = base, 2
        while folder.exists():
            folder, n = base.with_name(f"{base.name}_{n}"), n + 1
        return folder

    def latest(self, proj: dict[str, Any]) -> Path | None:
        """The project's newest export folder, if it was ever exported."""
        found = [p for p in self.output.glob(f"*/*_{proj['number']:02d}_{proj['slug']}*")
                 if p.is_dir() and not p.name.endswith(TMP_SUFFIX)]
        return max(found, key=lambda p: p.stat().st_mtime, default=None)

    def export(self, proj: dict[str, Any], render: dict[str, Any]) -> Path:
        script = self.scripts.current(proj)
        current = self.specs.current(proj) or {}
        spec = self.specs.check(proj, current["spec"])[0]
        timings = self.voice.timings(proj)
        folder = self._folder(proj)
        tmp = folder.with_name(folder.name + TMP_SUFFIX)
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            shutil.copy2(render["path"], tmp / "video.mp4")
            (tmp / "caption.txt").write_text(
                caption_text(script["post_caption"], script["hashtags"]), encoding="utf-8")
            at = min(spec.cover.at_seconds, max(0.0, render["duration_s"] - 0.05))
            cover = grab_frame(Path(render["path"]), at, (1080, 1920))
            if cover is not None:
                cover.save(tmp / "cover.jpg", format="JPEG", quality=90)
            (tmp / "captions.srt").write_text(build_srt(timings, spec.captions.overrides),
                                              encoding="utf-8")
            info = {
                "fvs_version": __version__,
                "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "project": project_public(proj), "script": script, "spec": current,
                "voiceover": {"id": proj["voiceover_id"],
                              "duration_s": timings.get("duration_s")},
                "render": {"id": render["id"], "spec_hash": render["spec_hash"],
                           "duration_s": render["duration_s"],
                           "qa": json.loads(render["qa_report"]) if render["qa_report"]
                           else None}}
            (tmp / "project.json").write_text(json.dumps(info, indent=1, ensure_ascii=False),
                                              encoding="utf-8")
            os.replace(tmp, folder)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return folder
