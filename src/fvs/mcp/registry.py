"""The tool registry: one place that turns a service call into an MCP result (DS 8.2).

Every tool goes through ``Registry.call``, which:
- validates the arguments against the tool's Pydantic model (unknown fields refused),
  returning ``SPEC_INVALID`` with every problem and a correct example (MCP-5);
- runs the handler (in a thread unless it is async), with a progress callback;
- shapes the result: a one-line summary, the data as JSON text and as structured content;
  image tools return JPEG images plus the JSON text only (ADR-8);
- keeps the result under 1 MB, shrinking images if needed (MCP-4);
- turns any error into ``{code, message, fix, example}`` with the error flag set;
- writes one activity line for the call unless the service already wrote one (MCP-11).
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from PIL import Image
from pydantic import BaseModel, ConfigDict, ValidationError

from fvs.activity import counting_lines
from fvs.core import Core
from fvs.errors import ErrorCode, FvsError
from fvs.logs import get_logger
from fvs.see.service import Seen

log = get_logger("fvs.mcp")

MAX_RESULT_BYTES = 1_000_000
SLOW_CALL_S = 20.0  # MCP-3: every tool answers within this
OUTPUT_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}},
                 "required": ["summary"]}

Progress = Callable[[float, str | None], Awaitable[None]]


class Args(BaseModel):
    """Base for tool arguments: unknown fields are an error, not silently ignored."""

    model_config = ConfigDict(extra="forbid")


@dataclass
class Tool:
    name: str
    title: str
    description: str
    args: type[Args]
    handler: Callable[..., Any]
    read_only: bool = False
    idempotent: bool = False
    costs_money: bool = False
    images: bool = False
    example: str | None = None

    def definition(self) -> dict[str, Any]:
        schema = self.args.model_json_schema()
        schema.pop("title", None)
        out: dict[str, Any] = {
            "name": self.name, "title": self.title, "description": self.description,
            "inputSchema": schema,
            "annotations": {"title": self.title, "readOnlyHint": self.read_only,
                            "destructiveHint": False, "idempotentHint": self.idempotent,
                            "openWorldHint": self.costs_money}}
        if not self.images:
            out["outputSchema"] = OUTPUT_SCHEMA
        return out


@dataclass
class Registry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def definitions(self) -> list[dict[str, Any]]:
        return [t.definition() for t in self.tools.values()]

    async def call(self, core: Core, name: str, arguments: dict[str, Any] | None,
                   progress: Progress | None = None) -> dict[str, Any]:
        tool = self.tools[name]
        started = time.monotonic()
        with counting_lines() as lines:
            try:
                args = _parse(tool, arguments or {})
                if inspect.iscoroutinefunction(tool.handler):
                    out = await tool.handler(core, args, progress)
                else:
                    out = await asyncio.to_thread(tool.handler, core, args)
                result = shape(out)
                summary = out.data["summary"] if isinstance(out, Seen) else out["summary"]
            except FvsError as exc:
                result, summary = error_result(exc), f"{name} failed: {exc.message}"
            except Exception as exc:  # noqa: BLE001 - every failure becomes an MCP error
                log.exception("tool_crashed", tool=name)
                err = FvsError(ErrorCode.INTERNAL, f"{name} hit an unexpected error: {exc}",
                               "Try again; if it repeats, run `fvs doctor` and check the "
                               "core log in the workspace logs folder.")
                result, summary = error_result(err), f"{name} failed: {exc}"
            if lines[0] == 0:
                project_id = _project_id(core, arguments or {})
                core.activity.add(summary, source="claude", tool=name, project_id=project_id)
        seconds = time.monotonic() - started
        if seconds > SLOW_CALL_S:
            log.warning("tool_slow", tool=name, seconds=round(seconds, 1))
        return result


def _parse(tool: Tool, arguments: dict[str, Any]) -> Args:
    try:
        return tool.args.model_validate(arguments)
    except ValidationError as exc:
        details = [{"path": ".".join(str(p) for p in e["loc"]) or "(arguments)",
                    "problem": e["msg"]} for e in exc.errors()]
        first = details[0]
        raise FvsError(ErrorCode.SPEC_INVALID,
                       f"{tool.name}: {first['path']}: {first['problem']}"
                       + (f" (and {len(details) - 1} more)" if len(details) > 1 else ""),
                       "Fix the arguments listed in details; the schema is in tools/list.",
                       example=tool.example, details=details) from None


def _project_id(core: Core, arguments: dict[str, Any]) -> str | None:
    ref = arguments.get("project")
    if ref is None:
        return None
    try:
        return core.projects.get_row(ref)["id"]
    except FvsError:
        return None


# ---- result shapes ----------------------------------------------------------------------


def _text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def error_result(exc: FvsError) -> dict[str, Any]:
    return {"content": [_text(_dumps(exc.to_dict()))], "isError": True}


def _shrink(jpeg: bytes, budget: int) -> bytes:
    img = Image.open(io.BytesIO(jpeg))
    quality = 75
    while len(jpeg) > budget and (quality > 40 or max(img.size) > 256):
        if quality > 40:
            quality -= 10
        else:
            img = img.resize((max(1, img.width * 3 // 4), max(1, img.height * 3 // 4)))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality)
        jpeg = buf.getvalue()
    return jpeg


def shape(out: dict[str, Any] | Seen) -> dict[str, Any]:
    if isinstance(out, Seen):
        text = _dumps(out.data)
        images = list(out.images)
        budget = (MAX_RESULT_BYTES - len(text) - 2_000) * 3 // 4  # base64 grows by 4/3
        per = budget // max(1, len(images))
        images = [img if len(img) <= per else _shrink(img, per) for img in images]
        content = [{"type": "image", "mimeType": "image/jpeg",
                    "data": base64.b64encode(img).decode()} for img in images]
        return {"content": [*content, _text(text)], "isError": False}
    text = _dumps(out)
    if 2 * len(text.encode()) > MAX_RESULT_BYTES:  # the JSON goes out twice
        raise FvsError(ErrorCode.INTERNAL, "This result is larger than 1 MB.",
                       "Ask for less: use response_format \"concise\", a page (cursor), or "
                       "fewer parts.")
    return {"content": [_text(out["summary"]), _text(text)],
            "structuredContent": json.loads(text), "isError": False}
