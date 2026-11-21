"""The ElevenLabs HTTP client (VO-1, VO-7, DS 10.2). Only the voice service uses it.

- ``POST /v1/text-to-speech/{voice_id}/with-timestamps`` for one sentence at a time.
- ``GET /v1/user/subscription`` for the quota left and its reset time.
- At most 2 requests at once across the whole app (DS 10.5).
- 429, 5xx and network errors: up to 3 retries after 2, 4 and 8 s (or Retry-After),
  then ``VOICE_BUSY``. Key, quota, length and input errors are never retried.
"""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from fvs.errors import ErrorCode, FvsError

API_URL = "https://api.elevenlabs.io"
SUBSCRIPTION_URL = f"{API_URL}/v1/user/subscription"
OUTPUT_FORMAT = "mp3_44100_128"  # available on every plan
RETRY_WAITS_S = (2.0, 4.0, 8.0)
MAX_PARALLEL = 2

KEY_FIX = "Paste a current ElevenLabs API key in Settings (or run `fvs key`)."


@dataclass
class Speech:
    audio: bytes  # MP3
    alignment: dict[str, list[Any]]  # characters, character_start/end_times_seconds
    request_id: str | None
    chars_billed: int


@dataclass
class Quota:
    limit: int
    used: int
    resets_at: str | None  # ISO 8601 UTC

    @property
    def left(self) -> int:
        return max(0, self.limit - self.used)


def verify_key(key: str) -> None:
    """Check a key with ElevenLabs before it is saved (Settings and `fvs key`)."""
    if len(key) < 20:
        raise FvsError(ErrorCode.SETTINGS_INVALID, "That does not look like an ElevenLabs "
                       "API key.", "Copy the whole key from your ElevenLabs account page and "
                       "paste it again.")
    try:
        resp = httpx.get(SUBSCRIPTION_URL, timeout=10, headers={"xi-api-key": key})
    except httpx.HTTPError:
        raise FvsError(ErrorCode.VOICE_BUSY, "Could not reach ElevenLabs to test the key; "
                       "nothing was saved.", "Check the internet connection and try "
                       "again.") from None
    if resp.status_code != 200:
        raise FvsError(ErrorCode.VOICE_AUTH, "ElevenLabs did not accept this key; nothing "
                       "was saved.", "Copy the key again from your ElevenLabs account page.")


class ElevenLabs:
    def __init__(self, api_key: Callable[[], str | None], *,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._api_key = api_key
        self._client = httpx.Client(base_url=API_URL, transport=transport,
                                    timeout=httpx.Timeout(60.0, connect=10.0))
        self._sleep = sleep
        self._slots = threading.BoundedSemaphore(MAX_PARALLEL)

    def _headers(self) -> dict[str, str]:
        key = self._api_key()
        if not key:
            raise FvsError(ErrorCode.VOICE_AUTH, "No ElevenLabs API key is saved.", KEY_FIX)
        return {"xi-api-key": key}

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = self._headers()
        for attempt in range(len(RETRY_WAITS_S) + 1):
            try:
                with self._slots:
                    resp = self._client.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                if attempt == len(RETRY_WAITS_S):
                    raise FvsError(ErrorCode.VOICE_BUSY,
                                   f"ElevenLabs could not be reached ({type(exc).__name__}).",
                                   "Check the internet connection, then try again.") from None
                self._sleep(RETRY_WAITS_S[attempt])
                continue
            if resp.status_code != 429 and resp.status_code < 500:
                return _raise_for_error(resp)
            if attempt == len(RETRY_WAITS_S):
                break
            self._sleep(_retry_after(resp) or RETRY_WAITS_S[attempt])
        wait = _retry_after(resp) or 30
        raise FvsError(ErrorCode.VOICE_BUSY,
                       f"ElevenLabs is busy ({_reason(resp)}); it still refused after "
                       f"{len(RETRY_WAITS_S)} retries.",
                       f"Try again in about {wait:.0f} seconds.")

    def speak(self, *, voice_id: str, model_id: str, text: str,
              voice_settings: dict[str, Any],
              previous_request_ids: list[str] | None = None) -> Speech:
        body: dict[str, Any] = {"text": text, "model_id": model_id,
                                "voice_settings": voice_settings}
        if previous_request_ids:
            body["previous_request_ids"] = previous_request_ids[-3:]
        resp = self._request("POST", f"/v1/text-to-speech/{voice_id}/with-timestamps",
                             params={"output_format": OUTPUT_FORMAT}, json=body)
        data = resp.json()
        billed = (resp.headers.get("x-character-count")
                  or resp.headers.get("character-cost"))
        return Speech(audio=base64.b64decode(data["audio_base64"]),
                      alignment=data.get("alignment") or data.get("normalized_alignment"),
                      request_id=resp.headers.get("request-id"),
                      chars_billed=int(billed) if billed else len(text))

    def quota(self) -> Quota:
        data = self._request("GET", "/v1/user/subscription").json()
        reset = data.get("next_character_count_reset_unix")
        return Quota(limit=int(data.get("character_limit", 0)),
                     used=int(data.get("character_count", 0)),
                     resets_at=(datetime.fromtimestamp(int(reset), UTC).isoformat()
                                if reset else None))


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _detail(resp: httpx.Response) -> tuple[str, str]:
    """(status, message) from an ElevenLabs error body."""
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return "", resp.text[:200]
    if isinstance(detail, dict):
        return str(detail.get("status", "")), str(detail.get("message", ""))
    if isinstance(detail, list) and detail:  # 422 validation errors
        first = detail[0]
        where = ".".join(str(p) for p in first.get("loc", [])[1:])
        return "validation_error", f"{where}: {first.get('msg', '')}".strip(": ")
    return "", str(detail or "")


def _reason(resp: httpx.Response) -> str:
    status, message = _detail(resp)
    return status or message or f"HTTP {resp.status_code}"


def _raise_for_error(resp: httpx.Response) -> httpx.Response:
    if resp.status_code < 400:
        return resp
    status, message = _detail(resp)
    if status == "quota_exceeded":
        raise FvsError(ErrorCode.VOICE_QUOTA,
                       f"The ElevenLabs character quota is used up. {message}".strip(),
                       "Wait for the quota reset date (fvs_voiceover_generate with "
                       "dry_run shows it), or add credits on elevenlabs.io.")
    if status == "invalid_api_key" or resp.status_code == 401:
        raise FvsError(ErrorCode.VOICE_AUTH,
                       f"ElevenLabs rejected the API key. {message}".strip(), KEY_FIX)
    if status == "max_character_limit_exceeded":
        raise FvsError(ErrorCode.VOICE_TEXT_TOO_LONG,
                       f"A sentence is too long for ElevenLabs. {message}".strip(),
                       "Split the sentence into shorter lines and save the script again.")
    raise FvsError(ErrorCode.VOICE_INPUT_INVALID,
                   f"ElevenLabs refused the request (HTTP {resp.status_code}): "
                   f"{message or status or 'no reason given'}",
                   "Check the voice profile (voice ID and model) and the sentence text.")
