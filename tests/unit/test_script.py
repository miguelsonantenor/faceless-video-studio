"""Projects, script versions, length estimate and lifecycle (PRJ-1, PRJ-2, PRJ-5, SCR-1–5,
DS 7)."""

from __future__ import annotations

import json

import pytest

from fvs.core import Core
from fvs.errors import ErrorCode, FvsError
from fvs.projects import next_state, slugify
from tests.conftest import add_clip, make_project

SCRIPT = ("The volcano makes its own *lightning*.\n"
          "It happens {1,000|one thousand} times a year. [pause 0.6]\n"
          "Nobody knows exactly why.")


def _words(n: int) -> str:
    """n words, 10 per line."""
    words = [f"word{i}" for i in range(n)]
    return "\n".join(" ".join(words[i:i + 10]) + "." for i in range(0, n, 10))


def _state(core: Core, number: int) -> str:
    return core.projects.get_row(number)["state"]


def test_create_project_from_clips(core: Core) -> None:
    a, b = add_clip(core, "Volcano_Lightning_04.mp4"), add_clip(core)
    p = core.projects.create(clips=[a, b, a])
    assert (p["number"], p["title"], p["slug"], p["state"]) == (
        1, "Volcano Lightning", "volcano-lightning", "new")
    assert [c["number"] for c in core.projects.clips(p["id"])] == [a, b]
    assert core.projects.folder(core.projects.get_row(1)).name == "01-volcano-lightning"


def test_slugify() -> None:
    assert slugify("Crème Brûlée: 5 Facts!") == "creme-brulee-5-facts"
    assert slugify("火山") == "video"


def test_save_numbers_sentences_estimates_and_moves_to_scripted(core: Core) -> None:
    p = make_project(core, "Volcano")
    r = core.scripts.save(p["number"], voiceover=SCRIPT, hook="This volcano makes lightning",
                          hashtags="volcano, #science science", post_caption="Wow")
    assert (r["version"], r["unchanged"], r["title"]) == (1, False, "Volcano")
    assert [s["i"] for s in r["sentences"]] == [1, 2, 3]
    assert r["sentences"][1] == {"i": 2, "caption": "It happens 1,000 times a year.",
                                 "spoken": "It happens one thousand times a year.",
                                 "pause_after_s": 0.6, "emphasis": []}
    assert r["sentences"][0]["emphasis"] == [5]
    assert r["hashtags"] == ["#volcano", "#science"]
    # 17 spoken words at 150 wpm = 6.8 s, plus the 0.6 s pause.
    assert r["est_seconds"] == 7.4 and r["fit"] == "too_short"
    assert "Add about" in r["warnings"][0]
    assert _state(core, p["number"]) == "scripted"
    assert core.activity.recent(1)[0]["summary"].startswith("Saved script v1 for #1")


def test_same_script_twice_is_one_version(core: Core) -> None:
    p = make_project(core)
    core.scripts.save(p["number"], voiceover=SCRIPT)
    again = core.scripts.save(p["number"], voiceover=SCRIPT)
    assert (again["version"], again["unchanged"]) == (1, True)
    changed = core.scripts.save(p["number"], voiceover=SCRIPT + "\nOne more.")
    assert (changed["version"], changed["unchanged"]) == (2, False)


def test_versions_are_never_changed(core: Core) -> None:
    p = make_project(core)
    core.scripts.save(p["number"], voiceover=SCRIPT)
    with core.db.connect() as conn, pytest.raises(Exception, match="never changed"):
        conn.execute("UPDATE script_versions SET title = 'x'")


def test_fit_uses_the_settings(core: Core) -> None:
    p = make_project(core)
    r = core.scripts.save(p["number"], voiceover=_words(90))  # 36 s
    assert (r["est_seconds"], r["fit"], r["warnings"]) == (36.0, "fits", [
        "No hook: add one of 8 words or fewer to show in the first seconds (CAP-4)."])
    core.settings.update({"video.target_max_seconds": 32})
    r = core.scripts.save(p["number"], voiceover=_words(90) + "\nAgain.")
    assert r["fit"] == "too_long" and "Cut about" in r["warnings"][0]
    assert r["target"] == {"min_seconds": 30, "max_seconds": 32, "hard_max_seconds": 60}


def test_longer_than_the_maximum_is_refused_with_words_to_cut(core: Core) -> None:
    p = make_project(core)
    with pytest.raises(FvsError) as info:
        core.scripts.save(p["number"], voiceover=_words(170))  # 68 s > 60 s
    assert info.value.code == ErrorCode.SCRIPT_TOO_LONG
    assert "Cut about 20 words" in info.value.fix
    assert _state(core, p["number"]) == "new"


def test_hook_over_8_words_is_refused(core: Core) -> None:
    p = make_project(core)
    with pytest.raises(FvsError, match="at most 8"):
        core.scripts.save(p["number"], voiceover=SCRIPT,
                          hook="one two three four five six seven eight nine")


def test_new_script_marks_voiceover_and_render_stale(core: Core) -> None:
    p = make_project(core)
    core.scripts.save(p["number"], voiceover=SCRIPT)
    with core.db.connect() as conn:
        conn.execute("UPDATE projects SET state = 'in_review', voiceover_id = 'v', "
                     "render_id = 'r' WHERE id = ?", (p["id"],))
    core.scripts.save(p["number"], voiceover=SCRIPT + "\nRevised.")
    row = core.projects.get_row(p["number"])
    assert row["state"] == "scripted"
    assert json.loads(row["stale_flags"]) == ["render", "voiceover"]


@pytest.mark.parametrize("state", ["exported", "skipped"])
def test_exported_or_skipped_projects_refuse_a_script(core: Core, state: str) -> None:
    p = make_project(core)
    with core.db.connect() as conn:
        conn.execute("UPDATE projects SET state = ? WHERE id = ?", (state, p["id"]))
    with pytest.raises(FvsError) as info:
        core.scripts.save(p["number"], voiceover=SCRIPT)
    assert info.value.code == ErrorCode.WRONG_STATE and "Reopen #1" in info.value.fix


def test_lifecycle_table_matches_the_diagram() -> None:
    def go(state: str, event: str) -> str:
        return next_state({"state": state, "number": 1}, event)

    assert go("new", "script_saved") == "scripted"
    assert go("scripted", "voiceover_made") == "voiced"
    assert go("in_review", "voiceover_made") == "planned"
    assert go("voiced", "spec_saved") == "planned"
    assert go("changes_requested", "spec_saved") == "planned"
    assert go("planned", "render_started") == "rendering"
    assert go("rendering", "render_done") == "claude_check"
    assert go("claude_check", "ready") == "in_review"
    assert go("in_review", "approve") == "exported"
    assert go("in_review", "reject") == "changes_requested"
    assert go("changes_requested", "script_saved") == "scripted"
    assert go("exported", "reopen") == "planned"
    assert go("skipped", "reopen") == "new"
    for state, event in [("scripted", "skip"), ("new", "voiceover_made"),
                         ("scripted", "spec_saved"), ("exported", "spec_saved")]:
        with pytest.raises(FvsError):
            go(state, event)


def test_unknown_project_is_not_found(core: Core) -> None:
    with pytest.raises(FvsError) as info:
        core.scripts.save(9, voiceover=SCRIPT)
    assert info.value.code == ErrorCode.NOT_FOUND
