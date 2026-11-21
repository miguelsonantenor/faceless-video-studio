"""The ElevenLabs client: requests, retries and error mapping (VO-1, VO-7, DS 10.2)."""

from __future__ import annotations

import httpx
import pytest

from fvs.errors import ErrorCode, FvsError
from tests.fake_elevenlabs import FakeElevenLabs, error

SETTINGS = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "speed": 1.0,
            "use_speaker_boost": True}


def _speak(fake: FakeElevenLabs, **kw: object):
    return fake.client().speak(voice_id="v1", model_id="eleven_multilingual_v2",
                               text="Hello there.", voice_settings=SETTINGS, **kw)


def test_speak_sends_the_sentence_and_reads_audio_timings_and_cost() -> None:
    fake = FakeElevenLabs()
    speech = _speak(fake, previous_request_ids=["a", "b", "c", "d"])
    call = fake.calls[0]
    assert call["path"] == "/v1/text-to-speech/v1/with-timestamps"
    assert call["output_format"] == "mp3_44100_128" and call["key"] == "sk_test_key"
    assert call["previous_request_ids"] == ["b", "c", "d"]
    assert call["voice_settings"] == SETTINGS
    assert speech.audio == b"ID3-fake-mp3" and speech.request_id == "req1"
    assert speech.chars_billed == 12 and len(speech.alignment["characters"]) == 12


def test_busy_is_retried_with_backoff_or_retry_after() -> None:
    fake = FakeElevenLabs()
    fake.queued = [error(429, {"status": "system_busy"}),
                   error(503, "down", **{"retry-after": "1.5"}),
                   httpx.ConnectError("no route")]
    assert _speak(fake).request_id == "req1"
    assert fake.sleeps == [2.0, 1.5, 8.0]


def test_busy_after_three_retries_is_voice_busy() -> None:
    fake = FakeElevenLabs()
    fake.queued = [error(429, {"status": "too_many_concurrent_requests"})] * 4
    with pytest.raises(FvsError) as info:
        _speak(fake)
    assert info.value.code == ErrorCode.VOICE_BUSY and "too_many_concurrent" in info.value.message
    assert fake.sleeps == [2.0, 4.0, 8.0] and not fake.calls


@pytest.mark.parametrize(("response", "code"), [
    (error(401, {"status": "invalid_api_key", "message": "Invalid key"}), ErrorCode.VOICE_AUTH),
    (error(401, {"status": "quota_exceeded", "message": "0 left"}), ErrorCode.VOICE_QUOTA),
    (error(400, {"status": "max_character_limit_exceeded"}), ErrorCode.VOICE_TEXT_TOO_LONG),
    (error(422, [{"loc": ["body", "voice_settings", "speed"], "msg": "too high"}]),
     ErrorCode.VOICE_INPUT_INVALID),
    (error(404, {"status": "voice_not_found", "message": "No such voice"}),
     ErrorCode.VOICE_INPUT_INVALID),
])
def test_errors_are_mapped_and_never_retried(response: httpx.Response,
                                             code: ErrorCode) -> None:
    fake = FakeElevenLabs()
    fake.queued = [response]
    with pytest.raises(FvsError) as info:
        _speak(fake)
    assert info.value.code == code and info.value.fix
    assert fake.sleeps == []


def test_422_names_the_field() -> None:
    fake = FakeElevenLabs()
    fake.queued = [error(422, [{"loc": ["body", "voice_settings", "speed"], "msg": "too high"}])]
    with pytest.raises(FvsError, match="voice_settings.speed: too high"):
        _speak(fake)


def test_no_key_is_voice_auth_without_a_request() -> None:
    fake = FakeElevenLabs()
    with pytest.raises(FvsError) as info:
        fake.client(key=None).quota()
    assert info.value.code == ErrorCode.VOICE_AUTH and "fvs key" in info.value.fix


def test_quota() -> None:
    q = FakeElevenLabs().client().quota()
    assert (q.limit, q.left, q.resets_at) == (100_000, 99_000, "2026-01-01T00:00:00+00:00")


def test_quota_is_one_try_without_retry_waits() -> None:
    """The quota is only shown on screens; a busy ElevenLabs must not hold a thread 14 s."""
    fake = FakeElevenLabs()
    fake.queued.append(httpx.Response(503))
    with pytest.raises(FvsError) as info:
        fake.client().quota()
    assert info.value.code == ErrorCode.VOICE_BUSY and fake.sleeps == []
