"""Creator-editable settings (BUILD_PLAN section 1)."""

from __future__ import annotations

import pytest

from fvs.core import Core
from fvs.errors import ErrorCode, FvsError
from fvs.settings import SettingsService
from tests.conftest import capture_events


def test_defaults_match_the_plan(core: Core) -> None:
    s = core.settings.get()
    assert (s.video.target_min_seconds, s.video.target_max_seconds) == (30, 45)
    assert s.video.max_seconds == 60
    assert s.voice.daily_budget_chars == 20_000
    assert s.voice.budget_reset_time == "00:00"
    assert s.export.destination == "folder"
    assert (s.safe_zone.top, s.safe_zone.bottom, s.safe_zone.left, s.safe_zone.right) == (
        108, 320, 60, 120)


def test_update_saves_logs_and_publishes(core: Core, monkeypatch: pytest.MonkeyPatch) -> None:
    events = capture_events(core, monkeypatch)
    core.settings.update({"voice.daily_budget_chars": 30_000})
    assert core.settings.get().voice.daily_budget_chars == 30_000
    line = core.activity.recent(1)[0]
    assert line["summary"] == (
        "Creator changed the daily voice budget (characters) from 20,000 to 30,000.")
    assert line["source"] == "creator"
    assert {"type": "settings_changed",
            "changed": {"voice.daily_budget_chars": 30_000}} in events


def test_nested_changes_and_claude_source(core: Core) -> None:
    core.settings.update({"video": {"max_seconds": 90, "target_max_seconds": 75}},
                         source="claude")
    s = core.settings.get()
    assert (s.video.max_seconds, s.video.target_max_seconds) == (90, 75)
    assert core.activity.recent(1)[0]["tool"] == "fvs_settings"


def test_values_survive_a_restart(core: Core) -> None:
    core.settings.update({"video.target_min_seconds": 20})
    fresh = SettingsService(core.db, core.activity, core.bus)
    fresh.seed()  # seeding never overwrites a stored value
    assert fresh.get().video.target_min_seconds == 20


@pytest.mark.parametrize(
    ("changes", "path", "fix_part"),
    [
        ({"video.target_min_seconds": 50}, "settings", "Change the values"),
        ({"video.target_max_seconds": 70}, "settings", "Change the values"),
        ({"voice.daily_budget_chars": -1}, "voice.daily_budget_chars", "from 0 to 10000000"),
        ({"voice.budget_reset_time": "25:00"}, "voice.budget_reset_time", "HH:MM"),
        ({"export.destination": "folder_and_tiktok_draft"}, "settings", ""),
    ],
)
def test_invalid_changes_are_refused_and_nothing_changes(
    core: Core, changes: dict, path: str, fix_part: str
) -> None:
    before = core.settings.get_flat()
    with pytest.raises(FvsError) as info:
        core.settings.update(changes)
    err = info.value
    assert err.code == ErrorCode.SETTINGS_INVALID
    assert err.details and err.details[0]["path"] == path
    assert fix_part in err.details[0]["fix"] or fix_part in err.fix
    assert core.settings.get_flat() == before


def test_min_above_max_names_both_fields(core: Core) -> None:
    with pytest.raises(FvsError) as info:
        core.settings.update({"video.target_min_seconds": 50})
    assert "target_min_seconds must be at most video.target_max_seconds" in info.value.message


def test_tiktok_draft_is_not_available_yet(core: Core) -> None:
    with pytest.raises(FvsError, match="phase 5"):
        core.settings.update({"export.destination": "folder_and_tiktok_draft"})


def test_unknown_key_is_refused(core: Core) -> None:
    with pytest.raises(FvsError) as info:
        core.settings.update({"elevenlabs.api_key": "sk_x"})
    assert "Unknown setting" in info.value.message
    assert "voice.daily_budget_chars" in info.value.fix


def test_unchanged_value_writes_nothing(core: Core) -> None:
    n = len(core.activity.recent(200))
    core.settings.update({"video.max_seconds": 60})
    assert len(core.activity.recent(200)) == n
