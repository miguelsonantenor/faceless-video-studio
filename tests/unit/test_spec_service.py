"""Saving Edit Specs and planning them (EDT-1, EDT-3, EDT-4, EDT-5, PRJ-5)."""

from __future__ import annotations

import json

import pytest

from fvs.core import Core
from fvs.errors import ErrorCode, FvsError
from fvs.spec.service import merge_patch
from tests.conftest import TIMINGS, add_clip, voiced_project, word

SPEC = {"project": 1, "template": "storytime_dark",
        "timeline": {"segments": [{"clip": 1, "in": 0, "cue": {"sentences": [1]}},
                                  {"clip": 1, "in": 5, "cue": {"until": "end"}}]}}


@pytest.fixture
def proj(core: Core) -> dict:
    return voiced_project(core, [add_clip(core, duration_s=20)])


def test_merge_patch_rfc7386() -> None:
    target = {"a": 1, "b": {"c": 2, "d": 3}, "e": [1, 2]}
    assert merge_patch(target, {"b": {"c": None, "x": 9}, "e": [3], "a": None}) == {
        "b": {"d": 3, "x": 9}, "e": [3]}


def test_save_applies_the_template_versions_and_moves_to_planned(core: Core,
                                                                  proj: dict) -> None:
    r = core.specs.save(1, spec=SPEC)
    assert (r["version"], r["unchanged"], r["state"]) == (1, False, "planned")
    assert [(s["out_start"], s["out_end"]) for s in r["timeline"]["segments"]] == [
        (0.0, 1.5), (1.5, 4.9)]
    saved = core.specs.current(core.projects.get_row(1))
    assert saved["spec"]["template"] == "storytime_dark@1"
    assert saved["spec"]["captions"]["style"] == "bold_yellow"  # from the template
    assert saved["spec"]["reframe"]["mode"] == "blur_fill"
    spec_file = core.projects.folder(core.projects.get_row(1)) / "specs" / "v1.json"
    assert json.loads(spec_file.read_text(encoding="utf-8")) == saved["spec"]


def test_same_spec_is_the_same_version_and_hash(core: Core, proj: dict) -> None:
    a = core.specs.save(1, spec=SPEC)
    reordered = {"timeline": SPEC["timeline"], "template": "storytime_dark@1", "project": "#1"}
    b = core.specs.save(1, spec=reordered)
    assert b["unchanged"] and (b["version"], b["spec_hash"]) == (1, a["spec_hash"])


def test_patch_makes_a_new_version_and_null_deletes(core: Core, proj: dict) -> None:
    core.specs.save(1, spec=SPEC)
    r = core.specs.save(1, patch={"captions": {"max_words": 2,
                                               "overrides": [{"word": 2, "text": "VOLCANO"}]}})
    assert r["version"] == 2
    spec = core.specs.current(core.projects.get_row(1))["spec"]
    assert spec["captions"]["max_words"] == 2 and spec["captions"]["style"] == "bold_yellow"
    core.specs.save(1, patch={"captions": {"overrides": None}})
    assert core.specs.current(core.projects.get_row(1))["spec"]["captions"]["overrides"] == []


def test_hash_changes_with_the_clip_content(core: Core, proj: dict) -> None:
    a = core.specs.save(1, spec=SPEC)["spec_hash"]
    with core.db.connect() as conn:
        conn.execute("UPDATE clips SET content_hash = 'other'")
    _, _, b, _ = core.specs.check(core.projects.get_row(1), dict(SPEC))
    assert a != b


def test_dry_run_returns_problems_and_saves_nothing(core: Core, proj: dict) -> None:
    bad = merge_patch(SPEC, {"timeline": {"segments": [
        {"clip": 1, "in": 19, "cue": {"sentences": [1, 3]}}]}})
    r = core.specs.save(1, spec=bad, dry_run=True)
    assert r["valid"] is False and r["error"]["code"] == "CUE_UNRESOLVED"
    assert r["errors"][0]["path"] == "timeline.segments[0]"
    ok = core.specs.save(1, spec=SPEC, dry_run=True)
    assert ok["valid"] and ok["timeline"]["segments"] and ok["spec"]["template"]
    row = core.projects.get_row(1)
    assert (row["spec_v"], row["state"]) == (None, "voiced")


def test_schema_errors_come_back_with_paths(core: Core, proj: dict) -> None:
    with pytest.raises(FvsError) as info:
        core.specs.save(1, spec={**SPEC, "captions": {"mode": "karaoke"}})
    assert info.value.code == ErrorCode.SPEC_INVALID
    assert info.value.details[0]["path"] == "captions.mode"


@pytest.mark.parametrize(("kwargs", "code"), [
    ({}, ErrorCode.SPEC_INVALID),
    ({"spec": SPEC, "patch": {}}, ErrorCode.SPEC_INVALID),
    ({"patch": {"captions": {}}}, ErrorCode.NOT_FOUND),
    ({"spec": {**SPEC, "project": 2}}, ErrorCode.SPEC_INVALID),
])
def test_bad_save_calls(core: Core, proj: dict, kwargs: dict, code: ErrorCode) -> None:
    with pytest.raises(FvsError) as info:
        core.specs.save(1, **kwargs)
    assert info.value.code == code


def test_no_spec_before_the_voiceover(core: Core) -> None:
    p = core.projects.create(clips=[add_clip(core)])
    core.scripts.save(p["number"], voiceover="Hello there.")
    with pytest.raises(FvsError) as info:
        core.specs.save(p["number"], spec=SPEC)
    assert info.value.code == ErrorCode.WRONG_STATE


# ---- the planner -----------------------------------------------------------------------


def _analysed(core: Core, number: int, scenes: list[tuple[float, float]],
              black: list[tuple[float, float]] = ()) -> None:
    clip = core.library.get_row(number)
    core.analyzer._write(clip, "analysis.json", {
        "scenes": [{"start": a, "end": b} for a, b in scenes],
        "black": [{"start": a, "end": b} for a, b in black], "frozen": [], "loudness": None})


def test_plan_uses_every_scene_before_reusing_and_validates(core: Core) -> None:
    a, b = add_clip(core, duration_s=12), add_clip(core, duration_s=6)
    _analysed(core, a, [(0, 4), (4, 8), (8, 12)])
    _analysed(core, b, [(0, 6)], black=[(2.0, 2.5)])
    voiced_project(core, [a, b])
    r = core.planner.propose(1)
    assert r["problems"] == [] and r["timeline"]["duration_s"] == 4.9
    segs = r["spec"]["timeline"]["segments"]
    assert segs[-1]["cue"] == {"until": "end"}
    assert [s["clip"] for s in segs] == [a, b, a]  # alternates clips
    assert segs[0]["in"] == 0.2 and segs[0]["motion"] == "zoom_in"
    assert r["spec"]["template"] == "storytime_dark@1" and not r["notes"]
    core.specs.save(1, spec=r["spec"])  # a proposal saves as it is


def test_plan_splits_long_sentences_into_word_shots(core: Core) -> None:
    long = {"sentences": [{"i": 1, "start": 0.0, "end": 8.0, "caption": "x", "spoken": "x",
                           "pause_after_s": 0.0,
                           "words": [word(f"w{k}", k * 0.8, k * 0.8 + 0.7) for k in range(10)]}]}
    n = add_clip(core, duration_s=30)
    _analysed(core, n, [(0, 10), (10, 20), (20, 30)])
    voiced_project(core, [n], timings=long)
    r = core.planner.propose(1)
    cues = [s["cue"] for s in r["spec"]["timeline"]["segments"]]
    assert len(cues) == 3 and cues[0] == {"words": [1, 4]} and cues[-1] == {"until": "end"}
    assert r["problems"] == []
    assert all(s["out_end"] - s["out_start"] <= 3.5
               for s in r["timeline"]["segments"])


def test_plan_notes_a_clip_that_is_too_short(core: Core) -> None:
    n = add_clip(core, duration_s=1.2)
    _analysed(core, n, [(0, 1.2)])
    voiced_project(core, [n])
    r = core.planner.propose(1, template="clean_light")
    assert r["problems"] == [] and r["template"] == "clean_light@1"
    assert any("short for" in note and "hold" in note for note in r["notes"])
    assert "hold_last" in {s["fit"] for s in r["spec"]["timeline"]["segments"]}


def test_plan_without_analysis_uses_the_whole_clip_and_says_so(core: Core) -> None:
    voiced_project(core, [add_clip(core, duration_s=20)])
    r = core.planner.propose(1, changes={"captions": {"max_words": 1}})
    assert r["problems"] == [] and r["spec"]["captions"]["max_words"] == 1
    assert "not analysed yet" in r["notes"][0]


def test_plan_needs_the_voiceover(core: Core) -> None:
    core.projects.create(clips=[add_clip(core)])
    with pytest.raises(FvsError) as info:
        core.planner.propose(1)
    assert info.value.code == ErrorCode.WRONG_STATE


def test_vertical_clips_get_a_centre_crop(core: Core) -> None:
    n = add_clip(core, duration_s=20)
    with core.db.connect() as conn:
        conn.execute("UPDATE clips SET width = 1080, height = 1920")
    voiced_project(core, [n])
    r = core.planner.propose(1)
    assert r["spec"]["reframe"]["mode"] == "center_crop"
    assert TIMINGS["sentences"][0]["i"] == 1  # the shared fixture is untouched
