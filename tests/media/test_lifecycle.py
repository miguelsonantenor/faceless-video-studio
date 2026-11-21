"""Batches, review, export and the web app's media routes on real media (BAT-1, EXP-1,
OVL-1, PRJ-3, CAP-7, AC-12, AC-14, REV-2)."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from fvs.api import create_app
from fvs.core import Core
from tests.conftest import wait_for
from tests.fake_elevenlabs import FakeElevenLabs, sine_mp3
from tests.media.conftest import number_of, open_core
from tests.media.test_render import SCRIPT, _ready, _render

pytestmark = pytest.mark.media


@pytest.fixture(scope="module")
def lcore(clips: dict[str, Path], tmp_path_factory: pytest.TempPathFactory) -> Iterator[Core]:
    base = tmp_path_factory.mktemp("life")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FVS_HOME", str(base / "dot-fvs"))
        mp.setenv("FVS_ROOT", str(base / "FVS"))
        core = open_core(base / "dot-fvs", base / "FVS", clips)
        core.library.scan()
        core.voice.client = FakeElevenLabs(mp3=sine_mp3).client()
        core.jobs.start()
        yield core
        core.jobs.stop(timeout=5)


def _finish(core: Core, job_id: str, timeout: float = 240) -> dict:
    wait_for(lambda: core.jobs.get(job_id)["state"] in ("done", "failed", "cancelled"),
             timeout=timeout)
    job = core.jobs.get(job_id)
    assert job["state"] == "done", job["error"]
    return job["result"]


@pytest.fixture(scope="module")
def batch(lcore: Core) -> dict:
    good = lcore.projects.create(clips=[number_of(lcore, "landscape.mp4")], title="Good")
    lcore.scripts.save(good["number"], voiceover=SCRIPT, hook="Volcanoes make lightning",
                       post_caption="Nature is wild", hashtags=["volcano", "science"])
    no_script = lcore.projects.create(clips=[number_of(lcore, "portrait.mp4")], title="Bad")
    started = lcore.batches.run([good["number"], no_script["number"]],
                                ["voiceover", "plan", "render"])
    return {"good": good["number"], "bad": no_script["number"],
            "result": _finish(lcore, started["job_id"])}


def test_bat1_one_failure_does_not_stop_the_others(lcore: Core, batch: dict) -> None:
    items = {i["project"]: i for i in batch["result"]["items"]}
    good, bad = items[batch["good"]], items[batch["bad"]]
    assert good["status"] == "done" and good["render"]["path"] and good["qa"]["status"]
    assert bad["status"] == "failed" and bad["error"]["code"] == "NOT_FOUND"
    assert lcore.projects.get_row(batch["good"])["state"] == "claude_check"
    bad_row = lcore.projects.get_row(batch["bad"])
    assert bad_row["failed"] == 1 and json.loads(bad_row["last_error"])["fix"]
    assert "1 failed" in batch["result"]["summary"]
    listed = lcore.reviews.list_projects(failed_only=True)["items"]
    assert [i["number"] for i in listed] == [batch["bad"]]


def test_exp1_approve_writes_the_package(lcore: Core, batch: dict) -> None:
    n = batch["good"]
    lcore.reviews.decide([n], "ready", override_qa=True, note="tone voice in tests")
    out = lcore.reviews.decide([n], "approve", by="creator")
    folder = Path(out["items"][0]["export"])
    assert folder.name.endswith(f"_{n:02d}_good") and folder.parent.parent == \
        lcore.config.folders.expanded().output
    proj = lcore.projects.get_row(n)
    assert proj["state"] == "exported"
    with lcore.db.connect() as conn:
        render = conn.execute("SELECT path FROM renders WHERE id = ?",
                              (proj["render_id"],)).fetchone()["path"]
    assert (folder / "video.mp4").read_bytes() == Path(render).read_bytes()  # no re-render
    assert (folder / "caption.txt").read_text(encoding="utf-8") == \
        "Nature is wild\n\n#volcano #science\n"
    assert Image.open(folder / "cover.jpg").size == (1080, 1920)
    srt = (folder / "captions.srt").read_text(encoding="utf-8")
    assert srt.count(" --> ") == 3 and "lightning" in srt
    info = json.loads((folder / "project.json").read_text(encoding="utf-8"))
    assert info["render"]["id"] == proj["render_id"] and info["script"]["version"] == 1
    assert not list(folder.parent.glob("*.fvs-tmp"))
    reopened = lcore.reviews.decide([n], "reopen", by="creator")
    assert reopened["items"][0]["state"] == "planned"


def _ledger(core: Core) -> int:
    with core.db.connect() as conn:
        return conn.execute("SELECT COALESCE(SUM(chars), 0) FROM cost_ledger").fetchone()[0]


def test_ac12_a_caption_fix_costs_no_characters(lcore: Core) -> None:
    n = _ready(lcore, ["landscape.mp4"])
    before_vo = lcore.projects.get_row(n)["voiceover_id"]
    before = _ledger(lcore)
    saved = lcore.specs.save(n, patch={"captions": {"overrides": [
        {"word": 2, "text": "VOLCANO!"}]}})
    assert not saved["unchanged"]
    result = _render(lcore, n)
    proj = lcore.projects.get_row(n)
    assert _ledger(lcore) == before and proj["voiceover_id"] == before_vo
    ass = next(p for p in (lcore.projects.folder(proj) / "captions").glob("*.ass")
               if p.stem == Path(result["path"]).stem)
    assert "VOLCANO!" in ass.read_text(encoding="utf-8")


def test_ac14_one_video_from_three_clips(lcore: Core) -> None:
    n = _ready(lcore, ["landscape.mp4", "portrait.mp4", "phone_vfr.mov"])
    proj = lcore.projects.get_row(n)
    timeline = lcore.specs.check(proj, lcore.specs.current(proj)["spec"])[1]
    assert len({s["clip"] for s in timeline["segments"]}) == 3


def test_ac14_three_projects_from_one_clip_with_their_own_ranges(lcore: Core) -> None:
    clip = number_of(lcore, "long_3min.mp4")
    starts = []
    for k, start in enumerate((5.0, 60.0, 120.0)):
        p = lcore.projects.create(clips=[clip], title=f"Part {k + 1}", series="Long",
                                  part=k + 1)
        lcore.scripts.save(p["number"], voiceover=SCRIPT)
        job = lcore.voice.generate(p["number"])
        if "job_id" in job:
            _finish(lcore, job["job_id"], timeout=60)
        lcore.specs.save(p["number"], spec={"timeline": {"segments": [
            {"clip": clip, "in": start, "cue": {"until": "end"}}]}})
        proj = lcore.projects.get_row(p["number"])
        timeline = lcore.specs.check(proj, lcore.specs.current(proj)["spec"])[1]
        starts.append(timeline["segments"][0]["src_in"])
        assert proj["series"] == "Long" and proj["part"] == k + 1
    assert starts == [5.0, 60.0, 120.0]


def test_rev2_the_web_app_plays_and_shows_the_render(lcore: Core, batch: dict) -> None:
    n = batch["good"]
    proj = lcore.projects.get_row(n)
    size = Path(lcore.perception.render_row(proj)["path"]).stat().st_size
    with TestClient(create_app(lcore, manage_lifecycle=False),
                    base_url="http://127.0.0.1:8787") as c:
        c.headers["Authorization"] = f"Bearer {lcore.token}"
        part = c.get(f"/api/renders/{proj['render_id']}/video",
                     headers={"Range": "bytes=0-1023"})  # the player seeks with ranges
        assert part.status_code == 206 and len(part.content) == 1024
        assert part.headers["content-range"] == f"bytes 0-1023/{size}"
        sheet = c.get(f"/api/renders/{proj['render_id']}/sheet.jpg")
        assert sheet.status_code == 200 and sheet.content[:2] == b"\xff\xd8"
        thumb = c.get(f"/api/projects/{n}/thumbs/0.jpg")
        assert thumb.status_code == 200
        assert Image.open(io.BytesIO(thumb.content)).size == (144, 256)
        assert c.get(f"/api/projects/{n}/thumbs/0.jpg").content == thumb.content  # cached
        spec = c.get(f"/api/projects/{n}/spec").json()
        assert spec["timeline"]["segments"]