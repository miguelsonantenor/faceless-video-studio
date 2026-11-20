"""MCP over Streamable HTTP at ``/mcp`` (MCP-10; DS 8.1).

Stateless and JSON only: each POST carries one JSON-RPC message and gets one JSON
response (202 with no body for notifications). The one exception is ``fvs_job_get``
called with a progress token by a client that accepts ``text/event-stream``: that answer
streams MCP progress notifications while it waits, then the result (DS 8.4). There is no
standalone server stream, so GET answers 405.

Token, Host, Origin and content-type checks happen in the security middleware that wraps
the whole app.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from fvs import __version__
from fvs.core import Core
from fvs.mcp.guide import PROMPTS, prompt_text
from fvs.mcp.registry import Registry
from fvs.mcp.tools import build_registry

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
INSTRUCTIONS = ("Faceless Video Studio turns the creator's clips into finished TikTok "
                "videos. Call fvs_guide first: it has the workflow, the script markup and "
                "the Edit Spec schema. fvs_voiceover_generate is the only tool that costs "
                "money.")


def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


class McpServer:
    def __init__(self, core: Core, registry: Registry) -> None:
        self.core = core
        self.registry = registry

    async def handle(self, msg: Any, progress: Any = None) -> dict[str, Any] | None:
        """One JSON-RPC message in, one response out (None for notifications)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Not a JSON-RPC 2.0 message.")
        if "method" not in msg or "id" not in msg:
            return None  # a notification, or a response to us: nothing to answer
        id_, method, params = msg["id"], msg["method"], msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"jsonrpc": "2.0", "id": id_, "result": {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS
                else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False},
                                 "prompts": {"listChanged": False}},
                "serverInfo": {"name": "fvs", "title": "Faceless Video Studio",
                               "version": __version__},
                "instructions": INSTRUCTIONS}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": id_, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": id_,
                    "result": {"tools": self.registry.definitions()}}
        if method == "tools/call":
            name = params.get("name")
            if name not in self.registry.tools:
                return _error(id_, -32602, f"Unknown tool {name!r}. See tools/list.")
            result = await self.registry.call(self.core, name, params.get("arguments"),
                                              progress)
            return {"jsonrpc": "2.0", "id": id_, "result": result}
        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": id_, "result": {"prompts": [
                {"name": n, "description": p["description"], "arguments": p["arguments"]}
                for n, p in PROMPTS.items()]}}
        if method == "prompts/get":
            name = params.get("name")
            if name not in PROMPTS:
                return _error(id_, -32602, f"Unknown prompt {name!r}. See prompts/list.")
            args = params.get("arguments") or {}
            missing = [a["name"] for a in PROMPTS[name]["arguments"]
                       if a["required"] and not args.get(a["name"])]
            if missing:
                return _error(id_, -32602, f"Prompt {name} needs: {', '.join(missing)}.")
            return {"jsonrpc": "2.0", "id": id_, "result": {
                "description": PROMPTS[name]["description"],
                "messages": [{"role": "user", "content": {
                    "type": "text", "text": prompt_text(name, args)}}]}}
        return _error(id_, -32601, f"Method {method!r} is not supported.")


def _wants_progress_stream(msg: Any, request: Request) -> Any:
    """The progress token, when this call should stream progress (DS 8.4)."""
    if not isinstance(msg, dict) or msg.get("method") != "tools/call":
        return None
    params = msg.get("params") or {}
    token = (params.get("_meta") or {}).get("progressToken")
    accepts = "text/event-stream" in request.headers.get("accept", "")
    return token if token is not None and accepts and \
        params.get("name") == "fvs_job_get" else None


def _sse(message: dict[str, Any]) -> str:
    return f"event: message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"


def add_mcp_routes(app: FastAPI, core: Core) -> None:
    server = McpServer(core, build_registry())

    @app.post("/mcp", include_in_schema=False)
    async def mcp_post(request: Request) -> Response:
        app.state.mcp_last_call = time.monotonic()  # "Claude connected" in the web app
        try:
            msg = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(_error(None, -32700, "The body is not valid JSON."),
                                status_code=400)
        token = _wants_progress_stream(msg, request)
        if token is None:
            reply = await server.handle(msg)
            return Response(status_code=202) if reply is None else JSONResponse(reply)

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def progress(fraction: float, note: str | None) -> None:
            params: dict[str, Any] = {"progressToken": token, "progress": fraction,
                                      "total": 1.0}
            if note:
                params["message"] = note
            await queue.put({"jsonrpc": "2.0", "method": "notifications/progress",
                             "params": params})

        task = asyncio.create_task(server.handle(msg, progress))

        async def stream() -> AsyncIterator[str]:
            while True:
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({getter, task},
                                             return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield _sse(getter.result())
                    continue
                getter.cancel()
                while not queue.empty():
                    yield _sse(queue.get_nowait())
                reply = task.result()
                if reply is not None:
                    yield _sse(reply)
                return

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.get("/mcp", include_in_schema=False)
    @app.delete("/mcp", include_in_schema=False)
    async def mcp_other() -> Response:
        return Response(status_code=405, headers={"Allow": "POST"})
