"""The Core API for the web app (REV-1–5): REST plus a live event stream.

WP0 covers health, status, settings, doctor, activity, jobs and events. Project, review
and asset routes come with their work packages; the MCP server mounts at ``/mcp`` in WP6
behind the same security middleware.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from fvs import __version__
from fvs.api.security import COOKIE_NAME, SecurityMiddleware, token_matches
from fvs.core import Core
from fvs.doctor import run_doctor, worst
from fvs.errors import ErrorCode, FvsError

SSE_HEARTBEAT_S = 15.0

_STATUS = {
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.PATH_NOT_ALLOWED: 403,
    ErrorCode.SETTINGS_INVALID: 422,
    ErrorCode.SPEC_INVALID: 422,
    ErrorCode.INTERNAL: 500,
}


def settings_payload(core: Core) -> dict[str, Any]:
    """Everything the Settings screen shows. The API key is reduced to its last 4."""
    folders = core.config.folders.expanded()
    return {
        "settings": core.settings.get_flat(),
        "elevenlabs": {
            "configured": core.config.elevenlabs_api_key is not None,
            "key_last4": core.config.api_key_last4,
        },
        "folders": {"input": str(folders.input), "workspace": str(folders.workspace),
                    "output": str(folders.output)},
        "mcp": {"address": f"http://127.0.0.1:{core.config.port}/mcp",
                "token_last4": core.token[-4:]},
    }


def create_app(core: Core, *, manage_lifecycle: bool = True,
               extra_origins: Iterable[str] = ()) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if manage_lifecycle:
            core.start()
        try:
            yield
        finally:
            if manage_lifecycle:
                core.stop()

    app = FastAPI(title="Faceless Video Studio", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.state.core = core

    @app.exception_handler(FvsError)
    async def _fvs_error(_req: Request, exc: FvsError) -> JSONResponse:
        return JSONResponse(exc.to_dict(), status_code=_STATUS.get(exc.code, 400))

    # ---- session ---------------------------------------------------------------------

    @app.get("/auth", include_in_schema=False)
    async def auth(t: str = Query("")) -> Any:
        """``fvs open`` sends the browser here once; the token becomes a session cookie."""
        if not token_matches(t, core.token):
            return JSONResponse({"code": "UNAUTHORIZED", "message": "Wrong or missing token.",
                                 "fix": "Open the web app with `fvs open`."}, status_code=401)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE_NAME, core.token, httponly=True, samesite="strict", path="/")
        return resp

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><title>Faceless Video Studio</title>"
            "<p>The FVS core is running. The web app arrives in WP8.</p>"
        )

    # ---- status ----------------------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return {
            "core": "running",
            "version": __version__,
            "schema_version": core.db.schema_version(),
            "workers": core.jobs.n_workers,
            "active_jobs": len(core.jobs.active()),
        }

    @app.get("/api/doctor")
    async def doctor(online: bool = False) -> dict[str, Any]:
        checks = await asyncio.to_thread(run_doctor, core.config, online=online)
        return {"status": worst(checks), "checks": [c.to_dict() for c in checks]}

    # ---- settings --------------------------------------------------------------------

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return settings_payload(core)

    @app.patch("/api/settings")
    async def patch_settings(changes: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        core.settings.update(changes, source="creator")
        return settings_payload(core)

    # ---- activity and jobs -----------------------------------------------------------

    @app.get("/api/activity")
    async def activity(limit: int = 50, before_id: int | None = None) -> dict[str, Any]:
        return {"items": core.activity.recent(limit, before_id)}

    @app.get("/api/jobs")
    async def jobs() -> dict[str, Any]:
        return {"items": core.jobs.active()}

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str) -> dict[str, Any]:
        return core.jobs.get(job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        return core.jobs.cancel(job_id)

    # ---- live events (REV-5) ---------------------------------------------------------

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            yield ": connected\n\n"
            async with core.bus.subscribe() as queue:
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), SSE_HEARTBEAT_S)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    app.add_middleware(SecurityMiddleware, token=core.token, port=core.config.port,
                       extra_origins=extra_origins)
    return app
