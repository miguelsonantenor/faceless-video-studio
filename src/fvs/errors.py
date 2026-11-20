"""The error model shared by every component (DS 8.6, MCP-5, NFR-U2).

Every error says what went wrong, how to fix it, and (where it helps) a correct example.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Codes in use so far. Each work package adds the DS 8.6 codes it raises."""

    # From DS 8.6:
    NOT_FOUND = "NOT_FOUND"
    SCRIPT_TOO_LONG = "SCRIPT_TOO_LONG"
    LANGUAGE_UNSUPPORTED = "LANGUAGE_UNSUPPORTED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    VOICE_AUTH = "VOICE_AUTH"
    VOICE_QUOTA = "VOICE_QUOTA"
    VOICE_TEXT_TOO_LONG = "VOICE_TEXT_TOO_LONG"
    VOICE_BUSY = "VOICE_BUSY"
    VOICE_INPUT_INVALID = "VOICE_INPUT_INVALID"
    SPEC_INVALID = "SPEC_INVALID"
    CUE_UNRESOLVED = "CUE_UNRESOLVED"
    RENDER_FAILED = "RENDER_FAILED"
    QA_FAILED = "QA_FAILED"
    ASSET_UNLICENSED = "ASSET_UNLICENSED"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    CORE_UNAVAILABLE = "CORE_UNAVAILABLE"
    # Not in DS 8.6, needed by the core itself:
    SCRIPT_INVALID = "SCRIPT_INVALID"  # markup or field rules (SCR-1, SCR-3)
    WRONG_STATE = "WRONG_STATE"  # the lifecycle (DS 7) does not allow the step now
    SETTINGS_INVALID = "SETTINGS_INVALID"
    JOB_INTERRUPTED = "JOB_INTERRUPTED"
    CLIP_UNREADABLE = "CLIP_UNREADABLE"
    MEDIA_TOOL_MISSING = "MEDIA_TOOL_MISSING"
    WHISPER_MODEL_MISSING = "WHISPER_MODEL_MISSING"
    INTERNAL = "INTERNAL"


class FvsError(Exception):
    """An error with a code, a plain-language message and a fix."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        fix: str,
        example: str | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.example = example
        self.details = details or []

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": str(self.code), "message": self.message, "fix": self.fix}
        if self.example is not None:
            out["example"] = self.example
        if self.details:
            out["details"] = self.details
        return out

    def __repr__(self) -> str:
        return f"FvsError({self.code}, {self.message!r})"
