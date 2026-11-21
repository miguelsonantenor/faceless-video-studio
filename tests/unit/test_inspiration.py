"""Inspiration sets (INS-1, INS-2): the folder, notes, the fingerprint and the measures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fvs.core import Core
from fvs.errors import FvsError
from fvs.inspiration import ROOT_SET, measure
from tests.conftest import add_clip, voiced_project, word


def _folder(core: Core) -> Path:
    return core.config.folders.expanded().inspiration


def test_measures_of_one_video() -> None:
    words = [word("Is", 0.4, 0.6), word("this", 0.6, 0.8), word("it?", 0.8, 1.2),
             word("Yes.", 1.5, 2.0), word("Follow", 9.0, 9.4), word("me.", 9.4, 9.8)]
    result = {"scenes": [{"start": 0, "end": 2}, {"start": 2, "end": 4},
                         {"start": 4, "end": 10}],
              "loudness": {"integrated_lufs": -14.2},
              "transcript": {"available": True, "segments": [
                  {"start": 0.4, "end": 2.0, "words": words[:4]},
                  {"start": 9.0, "end": 9.8, "words": words[4:]}]}}
    m = measure({"duration_s": 10.0}, result)
    assert (m["length_s"], m["shots"], m["median_shot_s"], m["cuts_per_s"]) == (10.0, 3, 2,
                                                                               0.2)
    assert m["first_word_s"] == 0.4 and m["words_per_minute"] == round(6 / 9.4 * 60)
    assert (m["hook"], m["hook_words"], m["hook_seconds"]) == ("Is this it?", 3, 0.8)
    assert m["ending"] == "Follow me." and m["words_per_sentence"] == 2
    assert m["speech_share"] == 0.24 and m["loudness_lufs"] == -14.2
    silent = measure({"duration_s": 5.0}, {"scenes": None, "transcript": None})
    assert silent["shots"] == 1 and silent["words_per_minute"] is None


def test_sets_are_subfolders_and_notes_survive_a_rename(core: Core) -> None:
    root = _folder(core)
    (root / "perfume").mkdir(parents=True)
    (root / "loose.mp4").write_bytes(b"one")
    (root / "perfume" / "b.mp4").write_bytes(b"two")
    (root / "perfume" / "a.mov").write_bytes(b"three")
    (root / "perfume" / "notes.txt").write_text("not a video")
    sets = {s["name"]: s for s in core.inspiration.scan()}
    assert set(sets) == {ROOT_SET, "perfume"}
    assert [v["file_name"] for v in sets["perfume"]["videos"]] == ["a.mov", "b.mp4"]

    core.inspiration.set_note("perfume", 2, "the fast hook")
    (root / "perfume" / "b.mp4").rename(root / "perfume" / "c.mp4")
    (root / "perfume" / "a.mov").unlink()
    videos = core.inspiration.scan()[1]["videos"]
    assert [(v["file_name"], v["note"]) for v in videos] == [("c.mp4", "the fast hook")]
    assert "the fast hook" in core.activity.recent(1)[0]["summary"]
    with pytest.raises(FvsError):
        core.inspiration.set_note("perfume", 5, "x")
    with pytest.raises(FvsError):
        core.inspiration.get("nope")


def test_the_fingerprint_follows_videos_and_notes(core: Core) -> None:
    root = _folder(core)
    (root / "a.mp4").write_bytes(b"one")
    core.inspiration.scan()
    fp = core.inspiration._fingerprint
    first = fp(core.inspiration.get(ROOT_SET))
    core.inspiration.set_note(ROOT_SET, 1, "captions")
    second = fp(core.inspiration.get(ROOT_SET))
    (root / "b.mp4").write_bytes(b"two")
    core.inspiration.scan()
    assert len({first, second, fp(core.inspiration.get(ROOT_SET))}) == 3
    assert core.inspiration.cached_profile(ROOT_SET) is None  # never computed on a read


def test_choosing_a_set(core: Core) -> None:
    assert core.inspiration.choose(None) is None  # nothing there yet
    data, sheet = core.inspiration.profile(None)
    assert data["status"] == "choose_set" and sheet is None and "No inspiration" in \
        data["summary"]
    (_folder(core) / "storytime").mkdir()
    (_folder(core) / "storytime" / "a.mp4").write_bytes(b"one")
    assert core.inspiration.choose(None) == "storytime"  # the only set with videos
    core.settings.update({"defaults.inspiration_set": "main"})
    assert core.inspiration.choose(None) == "main" and core.inspiration.choose("x") == "x"


def test_inspiration_videos_are_never_clips(core: Core) -> None:
    (_folder(core) / "a.mp4").write_bytes(b"one")
    core.inspiration.scan()
    core.library.scan()
    assert core.library.list()["items"] == []


def test_the_planner_paces_shots_like_the_examples(core: Core) -> None:
    """INS-3: the set's median shot replaces the template's longest shot."""
    long = {"sentences": [{"i": 1, "start": 0.0, "end": 8.0, "caption": "x", "spoken": "x",
                           "pause_after_s": 0.0,
                           "words": [word(f"w{k}", k * 0.8, k * 0.8 + 0.7) for k in range(10)]}]}
    voiced_project(core, [add_clip(core, duration_s=30)], timings=long)
    before = core.planner.propose(1)["spec"]["timeline"]["segments"]
    (_folder(core) / "a.mp4").write_bytes(b"one")
    core.inspiration.scan()
    s = core.inspiration.get(ROOT_SET)
    cache = core.inspiration._cache(s) / f"{core.inspiration._fingerprint(s)}.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"set": ROOT_SET, "target_shot_s": 0.8}), encoding="utf-8")
    core.settings.update({"defaults.inspiration_set": ROOT_SET})
    r = core.planner.propose(1)
    assert len(r["spec"]["timeline"]["segments"]) > len(before)
    assert any("median shot of the inspiration set" in n for n in r["notes"])
