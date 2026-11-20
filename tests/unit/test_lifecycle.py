"""Review decisions, the QA rule, restore, the project list, assets, batches' budget and
the export text files (PRJ-4, PRJ-5, QA-4, REV-3, BAT-2, BAT-3, CAP-7, AUD-4, NFR-L1)."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from fvs.core import Core
from fvs.errors import FvsError
from fvs.export.exporter import build_srt, caption_text, srt_time
from fvs.spec.models import Override
from fvs.timeutil import now_iso
from tests.conftest import TIMINGS, add_clip, make_project, voiced_project
from tests.fake_elevenlabs import FakeElevenLabs

SPEC = {"timeline": {"segments": [{"clip": 1, "in": 0, "cue": {"until": "end"}}]}}


def _rendered(core: Core, qa_status: str = "pass") -> dict:
    """A voiced project with a saved spec and a render row in Claude check."""
    n = add_clip(core)
    proj = voiced_project(core, [n])
    core.specs.save(proj["number"], spec={**SPEC, "timeline": {"segments": [
        {"clip": n, "in": 0, "cue": {"until": "end"}}]}})
    proj = core.projects.get_row(proj["number"])
    checks = [{"id": "loudness", "name": "Loudness", "status": qa_status,
               "measured": "-9.0 LUFS", "limit": "-14 ± 1 LUFS",
               "fix": "Lower audio.music.gain_db."}]
    rid = uuid.uuid4().hex
    current = core.specs.current(proj)
    with core.db.transaction() as conn:
        conn.execute("INSERT INTO renders (id, project_id, spec_hash, path, duration_s, bytes, "
                     "qa_report, created_at) VALUES (?, ?, ?, ?, 4.9, 1, ?, ?)",
                     (rid, proj["id"], current["spec_hash"], "/r.mp4",
                      json.dumps({"status": qa_status, "checks": checks, "summary": "x"}),
                      now_iso()))
        core.projects.advance(conn, proj, "render_started")
        core.projects.advance(conn, core.projects.get_row(proj["id"], conn), "render_done",
                              fields={"render_id": rid})
    return core.projects.get_row(proj["id"])


def _reviews(core: Core, pid: str) -> list[dict]:
    with core.db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM reviews WHERE project_id = ?",
                                              (pid,))]


def test_ready_with_a_failed_check_needs_an_override_note(core: Core) -> None:
    proj = _rendered(core, "fail")
    with pytest.raises(FvsError) as e:
        core.reviews.decide([proj["number"]], "ready")
    assert e.value.code == "QA_FAILED" and "Loudness" in e.value.message
    assert "audio.music.gain_db" in e.value.fix and "override_qa" in e.value.example
    with pytest.raises(FvsError) as e:
        core.reviews.decide([proj["number"]], "ready", override_qa=True)
    assert "note" in e.value.message
    out = core.reviews.decide([proj["number"]], "ready", override_qa=True,
                              note="loud on purpose")
    assert out["items"][0]["state"] == "in_review"
    row = _reviews(core, proj["id"])[-1]
    assert (row["decision"], row["qa_override"], row["note"], row["by"]) == \
        ("ready", 1, "loud on purpose", "claude")


def test_reject_needs_a_note_and_shows_in_the_list(core: Core) -> None:
    proj = _rendered(core)
    core.reviews.decide([proj["number"]], "ready")
    with pytest.raises(FvsError):
        core.reviews.decide([proj["number"]], "reject", by="creator")
    core.reviews.decide([proj["number"]], "reject", note="hook too slow", by="creator")
    listed = core.reviews.list_projects(state="changes_requested")
    assert listed["items"][0]["review_note"] == "hook too slow"
    assert listed["items"][0]["qa"] == "pass"
    attention = core.reviews.list_projects(needs_attention=True)["items"]
    assert [i["number"] for i in attention] == [proj["number"]]


def test_the_creator_approves_all_that_passed(core: Core) -> None:
    from fastapi.testclient import TestClient

    from fvs.api import create_app

    passed = _rendered(core)
    core.reviews.decide([passed["number"]], "ready")
    exported: list[int] = []
    core.reviews.exporter.export = lambda proj, render: (  # no real render file here
        exported.append(proj["number"]) or Path("/out/pkg"))
    with TestClient(create_app(core, manage_lifecycle=False),
                    base_url="http://127.0.0.1:8787") as c:
        c.headers["Authorization"] = f"Bearer {core.token}"
        r = c.post("/api/review/approve-passed")
        assert r.status_code == 200 and r.json()["items"][0]["state"] == "exported"
        bad = c.post("/api/review", json={"projects": [passed["number"]],
                                          "decision": "ready"})
        assert bad.status_code == 409 and bad.json()["code"] == "WRONG_STATE"
    assert exported == [passed["number"]]
    assert _reviews(core, passed["id"])[-1]["by"] == "creator"


def test_skip_and_reopen(core: Core) -> None:
    p = make_project(core)
    assert core.reviews.decide([p["number"]], "skip")["items"][0]["state"] == "skipped"
    assert core.reviews.decide([p["number"]], "reopen")["items"][0]["state"] == "new"
    with pytest.raises(FvsError) as e:
        core.reviews.decide([p["number"]], "approve")
    assert e.value.code == "WRONG_STATE"


def test_many_projects_report_each_result(core: Core) -> None:
    a, b = make_project(core), make_project(core)
    core.reviews.decide([a["number"]], "skip")
    out = core.reviews.decide([a["number"], b["number"]], "reopen")
    assert out["items"][0]["state"] == "new" and out["items"][1]["error"]["code"] == \
        "WRONG_STATE"
    assert "1 refused" in out["summary"]


def test_restore_a_script_version(core: Core) -> None:
    p = make_project(core)
    core.scripts.save(p["number"], voiceover="One sentence here.")
    core.scripts.save(p["number"], voiceover="Another sentence now.")
    out = core.reviews.restore(p["number"], script_version=1)
    assert out["project"]["script_version"] == 1
    assert out["project"]["state"] == "scripted" and "voiceover" in out["project"]["stale"]
    assert core.scripts.current(core.projects.get_row(p["number"]))["voiceover"] == \
        "One sentence here."
    with pytest.raises(FvsError):
        core.reviews.restore(p["number"], script_version=9)
    with pytest.raises(FvsError):
        core.reviews.restore(p["number"])


def test_restore_a_spec_version_reuses_its_render(core: Core) -> None:
    proj = _rendered(core)
    core.specs.save(proj["number"], patch={"captions": {"position": "center"}})
    assert core.projects.get_row(proj["number"])["state"] == "planned"
    out = core.reviews.restore(proj["number"], spec_version=1)
    assert out["project"]["state"] == "claude_check" and out["project"]["spec_version"] == 1
    assert "render" not in out["project"]["stale"]


def test_project_list_pages_and_counts(core: Core) -> None:
    for _ in range(23):
        make_project(core)
    first = core.reviews.list_projects()
    assert len(first["items"]) == 20 and first["next_cursor"] == 20
    assert first["counts"] == {"new": 23} and "23 project(s)" in first["summary"]
    rest = core.reviews.list_projects(cursor=first["next_cursor"])
    assert [i["number"] for i in rest["items"]] == [21, 22, 23]
    with pytest.raises(FvsError):
        core.reviews.list_projects(state="nope")


# ---- export text files -------------------------------------------------------------------


def test_srt_with_overrides() -> None:
    srt = build_srt(TIMINGS, [Override(word=2, text="VOLCANO!")])
    cues = srt.strip().split("\n\n")
    assert len(cues) == 3
    assert cues[0] == "1\n00:00:00,050 --> 00:00:01,400\nThe VOLCANO! erupts."
    assert srt_time(3725.5) == "01:02:05,500"


def test_caption_text() -> None:
    assert caption_text("Watch this", ["volcano", "#science", " "]) == \
        "Watch this\n\n#volcano #science\n"
    assert caption_text(None, []) == "\n"


# ---- assets -----------------------------------------------------------------------------


def _tone(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=f=220:d=2", str(path)], check=True)
    return path


def test_assets_need_a_license_and_an_allowed_path(core: Core) -> None:
    folders = core.config.folders.expanded()
    _tone(folders.input / "calm.mp3")
    with pytest.raises(FvsError) as e:
        core.assets.import_file("calm.mp3", kind="music", license=" ")
    assert e.value.code == "ASSET_UNLICENSED"
    with pytest.raises(FvsError) as e:
        core.assets.import_file(str(Path.home()), kind="music", license="CC0")
    assert e.value.code == "PATH_NOT_ALLOWED"
    out = core.assets.import_file("calm.mp3", kind="music", license="CC0",
                                  tags=["calm", "soft"])
    asset = out["asset"]
    assert asset["name"] == "calm" and abs(asset["duration_s"] - 2.0) < 0.1
    assert (folders.assets / "music" / "calm.mp3").exists()
    assert (folders.input / "calm.mp3").exists()  # the input is only read (LIB-2)
    with pytest.raises(FvsError):
        core.assets.import_file("calm.mp3", kind="music", license="CC0")  # name taken
    assert [a["name"] for a in core.assets.list_assets(kind="music", tag="calm")["items"]] \
        == ["calm"]
    assert core.assets.list_assets(tag="epic")["items"] == []


# ---- batches ----------------------------------------------------------------------------


def test_a_batch_over_budget_needs_permission(core: Core) -> None:
    core.voice.client = FakeElevenLabs().client()
    core.settings.update({"voice.daily_budget_chars": 10})
    p = make_project(core)
    core.scripts.save(p["number"], voiceover="This sentence costs more than ten characters.")
    with pytest.raises(FvsError) as e:
        core.batches.run([p["number"]], ["voiceover"])
    assert e.value.code == "BUDGET_EXCEEDED"
    out = core.batches.run([p["number"]], ["voiceover"], allow_over_budget=True)
    assert out["job_id"] and out["chars_to_bill"] > 10
    again = core.batches.run([p["number"]], ["voiceover"], allow_over_budget=True)
    assert again["job_id"] == out["job_id"]  # MCP-6: no duplicate job
    with pytest.raises(FvsError):
        core.batches.run([p["number"]], ["dance"])
