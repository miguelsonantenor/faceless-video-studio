"""The quota shown on every screen is kept for a minute, not fetched on each refresh."""

from __future__ import annotations

from fvs.core import Core
from tests.fake_elevenlabs import FakeElevenLabs


def test_quota_status_is_kept_and_fresh_asks_again(core: Core) -> None:
    fake = FakeElevenLabs()
    core.voice.client = fake.client()
    assert core.voice.quota_status()["left"] == 99_000
    fake.quota = {**fake.quota, "character_count": 5_000}
    assert core.voice.quota_status()["left"] == 99_000  # kept: no new request
    assert core.voice.quota_status(fresh=True)["left"] == 95_000  # before spending
    assert core.voice.quota_status()["left"] == 95_000
