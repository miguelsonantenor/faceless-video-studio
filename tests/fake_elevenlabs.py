"""A stand-in for the ElevenLabs API, served through httpx.MockTransport."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

import httpx

from fvs.voice.elevenlabs import ElevenLabs

SECONDS_PER_CHAR = 0.05


def even_alignment(text: str, seconds_per_char: float = SECONDS_PER_CHAR) -> dict:
    return {"characters": list(text),
            "character_start_times_seconds": [round(i * seconds_per_char, 3)
                                              for i in range(len(text))],
            "character_end_times_seconds": [round((i + 1) * seconds_per_char, 3)
                                            for i in range(len(text))]}


class FakeElevenLabs:
    def __init__(self, mp3: Callable[[float], bytes] | None = None) -> None:
        self.mp3 = mp3  # seconds -> MP3 bytes; fake bytes when None
        self.calls: list[dict] = []  # text-to-speech request bodies, in order
        self.queued: list[httpx.Response | Exception] = []  # served before normal answers
        self.sleeps: list[float] = []
        self.quota = {"character_count": 1_000, "character_limit": 100_000,
                      "next_character_count_reset_unix": 1_767_225_600}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.queued:
            nxt = self.queued.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        if request.url.path == "/v1/user/subscription":
            return httpx.Response(200, json=self.quota)
        body = json.loads(request.content)
        self.calls.append({**body, "path": request.url.path,
                           "output_format": request.url.params.get("output_format"),
                           "key": request.headers.get("xi-api-key")})
        text = body["text"]
        audio = self.mp3(len(text) * SECONDS_PER_CHAR) if self.mp3 else b"ID3-fake-mp3"
        return httpx.Response(
            200, json={"audio_base64": base64.b64encode(audio).decode(),
                       "alignment": even_alignment(text)},
            headers={"request-id": f"req{len(self.calls)}",
                     "x-character-count": str(len(text))})

    def client(self, key: str | None = "sk_test_key") -> ElevenLabs:
        return ElevenLabs(lambda: key, transport=httpx.MockTransport(self),
                          sleep=self.sleeps.append)


def error(status: int, detail: object, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail}, headers=headers)
