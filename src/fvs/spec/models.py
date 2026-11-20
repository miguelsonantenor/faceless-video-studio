"""The Edit Spec v1 schema (DS 5.1, 5.2; P1 fields only).

Claude can set anything this schema allows and nothing else (``extra="forbid"``
everywhere). The same models are the validator's first step and, in WP6, the JSON schema
``fvs_guide`` shows Claude.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEMA_VERSION = 1


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Cue(_M):
    """Exactly one of: sentences [a] or [a, b], words [from, to], seconds, until "end"."""

    sentences: list[int] | None = Field(None, min_length=1, max_length=2)
    words: list[int] | None = Field(None, min_length=2, max_length=2)
    seconds: float | None = Field(None, gt=0, le=60)
    until: Literal["end"] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Cue:
        set_ = [k for k in ("sentences", "words", "seconds", "until")
                if getattr(self, k) is not None]
        if len(set_) != 1:
            raise ValueError('a cue needs exactly one of "sentences", "words", "seconds" '
                             'or "until"')
        for name in ("sentences", "words"):
            pair = getattr(self, name)
            if pair is not None and (min(pair) < 1 or pair[0] > pair[-1]):
                raise ValueError(f"{name} must be numbers from 1, in order [from, to]")
        return self


class Transition(_M):
    type: Literal["cut", "fade"] = "cut"
    seconds: float = Field(0.3, ge=0.1, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def _from_word(cls, data: Any) -> Any:
        return {"type": data} if isinstance(data, str) else data


class Segment(_M):
    clip: int | str
    in_: float = Field(0.0, alias="in", ge=0)
    cue: Cue
    motion: Literal["none", "zoom_in", "zoom_out", "pan_left", "pan_right"] = "none"
    transition_in: Transition = Field(default_factory=Transition)
    fit: Literal["error", "slow", "hold_last", "loop"] = "error"


class Timeline(_M):
    segments: list[Segment] = Field(min_length=1, max_length=200)


class Reframe(_M):
    mode: Literal["center_crop", "blur_fill", "fit_bars", "manual"] = "blur_fill"
    focus_x: float = Field(0.5, ge=0, le=1)
    focus_y: float = Field(0.5, ge=0, le=1)
    zoom: float = Field(1.0, ge=1, le=2)


class OriginalAudio(_M):
    mode: Literal["mute", "duck", "keep"] = "duck"
    duck_db: float = Field(-14.0, ge=-40, le=0)  # level under the voice when ducking


class Music(_M):
    asset: str = Field(min_length=1)
    start_at: float = Field(0.0, ge=0)
    gain_db: float = Field(-18.0, ge=-40, le=0)
    duck: bool = True
    fade_in: float = Field(0.0, ge=0, le=10)
    fade_out: float = Field(1.5, ge=0, le=10)


class Loudness(_M):
    target_lufs: float = Field(-14.0, ge=-24, le=-9)
    true_peak_db: float = Field(-1.0, ge=-3, le=0)


class Audio(_M):
    original: OriginalAudio = Field(default_factory=OriginalAudio)
    music: Music | None = None
    loudness: Loudness = Field(default_factory=Loudness)


class Override(_M):
    word: int = Field(ge=1)  # caption word number across the whole voiceover
    text: str = Field(max_length=60)


class Captions(_M):
    style: str = "bold_yellow"
    mode: Literal["word_highlight", "chunks", "line", "off"] = "word_highlight"
    max_words: int = Field(3, ge=1, le=3)
    position: Literal["lower_third", "center", "upper_third"] = "lower_third"
    overrides: list[Override] = Field(default_factory=list)


class Hook(_M):
    text: str | None = Field(None, max_length=80)  # None: the script's hook
    seconds: float = Field(2.5, ge=0.5, le=5)


class End(_M):
    tail_seconds: float = Field(0.6, ge=0.3, le=1.0)


class Cover(_M):
    at_seconds: float = Field(1.0, ge=0)


class Output(_M):
    max_seconds: float = Field(60, ge=5, le=600)


class EditSpec(_M):
    schema_version: Literal[1] = SCHEMA_VERSION
    project: int | str
    template: str | None = None
    output: Output = Field(default_factory=Output)
    timeline: Timeline
    reframe: Reframe = Field(default_factory=Reframe)
    audio: Audio = Field(default_factory=Audio)
    captions: Captions = Field(default_factory=Captions)
    hook: Hook = Field(default_factory=Hook)
    end: End = Field(default_factory=End)
    cover: Cover = Field(default_factory=Cover)

    def dump(self) -> dict[str, Any]:
        """The canonical JSON form (``in``, not ``in_``; unset optional fields left out)."""
        return self.model_dump(by_alias=True, mode="json", exclude_none=True)


def json_path(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        part = "in" if part == "in_" else part
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "spec"


def _fix(err: dict[str, Any]) -> str:
    kind = err["type"]
    if kind == "extra_forbidden":
        return "Remove this field: the Edit Spec allows only the fields in fvs_guide's schema."
    if kind == "missing":
        return "Add this field."
    if kind == "literal_error":
        return f"Use one of: {err.get('ctx', {}).get('expected', 'the allowed values')}."
    ctx = err.get("ctx") or {}
    for key, word in (("ge", "at least"), ("gt", "more than"), ("le", "at most"),
                      ("lt", "less than")):
        if key in ctx:
            limit = ctx[key]
            return f"Use a value {word} {limit:g}." if isinstance(limit, int | float) \
                else f"Use a value {word} {limit}."
    return "See the problem and the example spec in fvs_guide."


def schema_errors(exc: ValidationError) -> list[dict[str, str]]:
    """Pydantic errors as {path, problem, fix} (DS 5.3 step 6)."""
    return [{"path": json_path(e["loc"]), "problem": e["msg"].removeprefix("Value error, "),
             "fix": _fix(e)} for e in exc.errors()]


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Bring an older spec up to the current schema (NFR-M1). v1 is the first."""
    data = dict(data)
    data.setdefault("schema_version", SCHEMA_VERSION)
    return data
