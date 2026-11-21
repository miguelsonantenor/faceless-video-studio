"""The routes the web app's screens read and act through (REV-1–5; BUILD_PLAN WP8).

Everything here is for the creator: decisions are recorded with ``by="creator"``. The
ElevenLabs key can be replaced but never read back; only its last 4 characters leave
the core. Renders are served with HTTP Range support so the player can seek.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from PIL import Image
from pydantic import SecretStr

from fvs.analyze.sheet import grab_frame
from fvs.atomic import write_text_atomic
from fvs.config import save_config
from fvs.core import Core
from fvs.doctor import claude_desktop_config_path
from fvs.errors import ErrorCode, FvsError
from fvs.logs import register_secret
from fvs.mcp.tools import ProjectGetArgs, project_get
from fvs.projects import STATE_LABELS, project_public
from fvs.see.stills import STILL_H, STILL_W
from fvs.voice.elevenlabs import verify_key

CLAUDE_SEEN_S = 300  # "Claude connected" when it called a tool in the last 5 minutes
THUMB = (STILL_W // 2, STILL_H // 2)
WEB_DIST = Path(__file__).resolve().parent.parent / "webapp"
# Set here, not guessed: Windows' registry can map .js to text/plain, which browsers refuse
# for module scripts.
WEB_TYPES = {".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2",
             ".woff": "font/woff", ".svg": "image/svg+xml", ".html": "text/html"}


def _length(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    m, s = divmod(round(seconds), 60)
    return f"{m}:{s:02d}"


def where_it_stands(row: dict[str, Any], *, job: dict[str, Any] | None,
                    qa: dict[str, Any] | None, note: str | None,
                    script: dict[str, Any] | None) -> str:
    """One plain line for the dashboard's "Where it stands" column."""
    if row["failed"] and row["last_error"]:
        return json.loads(row["last_error"])["message"]
    state = row["state"]
    if job is not None:
        if job["state"] == "queued":
            return f"{job['kind'].capitalize()} queued"
        return f"{job['kind'].capitalize()} {round(100 * (job['progress'] or 0))}%"
    if state in ("in_review", "claude_check") and qa is not None:
        bad = [c for c in qa.get("checks", []) if c["status"] == "fail"]
        if not bad:
            return "All 8 checks passed" if qa["status"] == "pass" else \
                "Checks passed with warnings"
        return f"{len(bad)} check(s) failed: " + ", ".join(c["name"].lower() for c in bad)
    if state == "changes_requested" and note:
        return f"Note: {note}"
    if state == "exported":
        return "Package in output folder"
    if state in ("scripted", "voiced") and script is not None:
        n = len(script["sentences"])
        return f"Script v{script['version']}, {n} sentence(s), about " \
               f"{script['est_seconds']:.0f} s"
    if state == "planned":
        return "Edit planned, ready to render"
    return STATE_LABELS[state]


def add_web_routes(app: FastAPI, core: Core) -> None:  # noqa: C901 - one route list
    def run(fn: Any, *args: Any, **kw: Any) -> Any:
        return asyncio.to_thread(fn, *args, **kw)

    def render_row(render_id: str) -> dict[str, Any]:
        with core.db.connect() as conn:
            row = conn.execute("SELECT * FROM renders WHERE id = ?", (render_id,)).fetchone()
        if row is None or not Path(row["path"]).exists():
            raise FvsError(ErrorCode.NOT_FOUND, "That render is not there any more.",
                           "Ask Claude to render the project again.")
        return dict(row)

    # ---- status --------------------------------------------------------------------

    @app.get("/api/overview")
    async def overview() -> dict[str, Any]:
        """Header and dashboard data in one call: counts, budget, jobs, activity."""
        last = getattr(app.state, "mcp_last_call", None)
        quota = await run(core.voice.quota_status)
        with core.db.connect() as conn:
            counts = {r["state"]: r["n"] for r in conn.execute(
                "SELECT state, COUNT(*) AS n FROM projects GROUP BY state")}
        return {"claude_connected": last is not None
                and time.monotonic() - last < CLAUDE_SEEN_S,
                "counts": {s: counts.get(s, 0) for s in STATE_LABELS},
                "labels": STATE_LABELS, "budget": core.budget.status(), "quota": quota,
                "jobs": core.jobs.active(), "activity": core.activity.recent(30)}

    # ---- projects ------------------------------------------------------------------

    @app.get("/api/projects")
    async def projects(state: str | None = None) -> dict[str, Any]:
        def build() -> dict[str, Any]:
            with core.db.connect() as conn:
                sql = "SELECT * FROM projects" + (" WHERE state = ?" if state else "") + \
                    " ORDER BY updated_at DESC LIMIT 200"
                rows = [dict(r) for r in conn.execute(sql, (state,) if state else ())]
                clips = {}
                for r in conn.execute(
                        "SELECT pc.project_id, c.file_name FROM project_clips pc JOIN clips c "
                        "ON c.id = pc.clip_id ORDER BY pc.position"):
                    clips.setdefault(r["project_id"], []).append(r["file_name"])
                renders = {r["id"]: dict(r) for r in conn.execute(
                    "SELECT id, duration_s, qa_report FROM renders")}
                notes = {r["project_id"]: r["note"] for r in conn.execute(
                    "SELECT project_id, note FROM reviews WHERE id IN "
                    "(SELECT MAX(id) FROM reviews GROUP BY project_id)")}
            jobs = {j["project_id"]: j for j in core.jobs.active() if j["project_id"]}
            items = []
            for r in rows:
                render = renders.get(r["render_id"]) if r["render_id"] else None
                qa = json.loads(render["qa_report"]) if render and render["qa_report"] \
                    else None
                script = core.scripts.current(r) if r["script_v"] else None
                items.append({
                    **project_public(r), "clips": clips.get(r["id"], []),
                    "where": where_it_stands(r, job=jobs.get(r["id"]), qa=qa,
                                             note=notes.get(r["id"]), script=script),
                    "length": _length(render["duration_s"]) if render else None,
                    "qa": qa and {"status": qa["status"], "summary": qa["summary"],
                                  "failed": [c for c in qa["checks"]
                                             if c["status"] == "fail"]},
                    "note": notes.get(r["id"]),
                    "updated_at": r["updated_at"]})
            return {"items": items}

        return await run(build)

    @app.get("/api/projects/{ref}")
    async def project(ref: str) -> dict[str, Any]:
        def build() -> dict[str, Any]:
            data = project_get(core, ProjectGetArgs(project=ref,
                                                    response_format="detailed"))
            proj = core.projects.get_row(ref)
            with core.db.connect() as conn:
                data["scripts"] = [dict(r) for r in conn.execute(
                    "SELECT version, voiceover_text, hook, author, est_seconds, created_at "
                    "FROM script_versions WHERE project_id = ? ORDER BY version",
                    (proj["id"],))]
                data["specs"] = [dict(r) for r in conn.execute(
                    "SELECT version, spec_hash, author, created_at FROM edit_specs WHERE "
                    "project_id = ? ORDER BY version", (proj["id"],))]
                vo = conn.execute("SELECT voice_alias, duration_s, chars_billed, "
                                  "chars_reused FROM voiceovers WHERE id = ?",
                                  (proj["voiceover_id"],)).fetchone() \
                    if proj["voiceover_id"] else None
                data["jobs"] = [dict(r) for r in conn.execute(
                    "SELECT id, kind, state, created_at FROM jobs WHERE project_id = ? "
                    "ORDER BY created_at DESC LIMIT 10", (proj["id"],))]
            data["voiceover"] = dict(vo) if vo else None
            return data

        return await run(build)

    @app.post("/api/projects/{ref}/restore")
    async def restore(ref: str, data: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        return await run(core.reviews.restore, ref,
                         script_version=data.get("script_version"),
                         spec_version=data.get("spec_version"), source="creator")

    @app.get("/api/projects/{ref}/spec")
    async def spec_json(ref: str, version: int | None = None) -> Response:
        proj = core.projects.get_row(ref)
        data = core.perception.spec_data(proj, version=version)
        return Response(json.dumps(data, indent=2, ensure_ascii=False),
                        media_type="application/json")

    @app.get("/api/projects/{ref}/thumbs/{index}.jpg")
    async def thumb(ref: str, index: int) -> Response:
        """A small frame of one segment, from the render if there is one (cached)."""
        def build() -> bytes:
            proj = core.projects.get_row(ref)
            current = core.specs.current(proj)
            if current is None:
                raise FvsError(ErrorCode.NOT_FOUND, "No Edit Spec yet.",
                               "Thumbnails appear once Claude saves an edit.")
            cache = core.projects.folder(proj) / "thumbs" / f"{current['spec_hash']}_" \
                f"{index}.jpg"
            if cache.exists():
                return cache.read_bytes()
            _, timeline, _, _ = core.specs.check(proj, current["spec"])
            seg = timeline["segments"][index]
            render = core.perception.render_row(proj) if proj["render_id"] else None
            if render is not None and render["spec_hash"] == current["spec_hash"]:
                img = grab_frame(Path(render["path"]), seg["out_start"] + 0.05, THUMB)
            else:
                seen = core.perception.frames(source="spec", project=ref, captions=False,
                                              times=[seg["out_start"] + 0.05])
                img = Image.open(io.BytesIO(seen.images[0]))
            buf = io.BytesIO()
            if img is not None:
                img.convert("RGB").resize(THUMB).save(buf, format="JPEG", quality=80)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(buf.getvalue())
            return buf.getvalue()

        return Response(await run(build), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})

    # ---- renders and logs ------------------------------------------------------------

    @app.get("/api/renders/{render_id}/video")
    async def video(render_id: str) -> FileResponse:
        return FileResponse(render_row(render_id)["path"], media_type="video/mp4")

    @app.get("/api/renders/{render_id}/sheet.jpg")
    async def sheet(render_id: str) -> FileResponse:
        path = render_row(render_id)["contact_sheet_path"]
        if not path or not Path(path).exists():
            raise FvsError(ErrorCode.NOT_FOUND, "This render has no contact sheet.",
                           "It is made by the quality checks after a render.")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/jobs/{job_id}/log")
    async def job_log(job_id: str) -> PlainTextResponse:
        job = core.jobs.get(job_id)
        logs = sorted(core.config.folders.expanded().workspace.rglob(f"{job['id']}.log"))
        text = logs[0].read_text(encoding="utf-8", errors="replace") if logs else \
            json.dumps(job, indent=2)
        return PlainTextResponse(text)

    # ---- the ElevenLabs key (never read back) ---------------------------------------

    @app.put("/api/key")
    async def replace_key(data: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        key = str(data.get("key", "")).strip()
        await asyncio.to_thread(verify_key, key)
        register_secret(key)
        core.config.elevenlabs_api_key = SecretStr(key)
        save_config(core.config, core.home)
        core.activity.add("Replaced the ElevenLabs key.", source="creator")
        return {"ok": True, "key_last4": core.config.api_key_last4,
                "quota": await run(core.voice.quota_status)}

    # ---- connecting Claude -----------------------------------------------------------

    def desktop_entry() -> dict[str, Any]:
        found = shutil.which("fvs")
        if found:
            return {"command": "fvs", "args": ["mcp"]}
        return {"command": sys.executable, "args": ["-m", "fvs", "mcp"]}

    @app.get("/api/claude-desktop")
    async def claude_desktop() -> dict[str, Any]:
        path = claude_desktop_config_path()
        try:
            servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
        except (OSError, json.JSONDecodeError):
            servers = {}
        port = core.config.port
        return {"path": str(path), "installed": "fvs" in servers,
                "config": {"mcpServers": {"fvs": desktop_entry()}},
                "mcp_address": f"http://127.0.0.1:{port}/mcp",
                "claude_code": f"claude mcp add --transport http fvs "
                               f"http://127.0.0.1:{port}/mcp --header "
                               "\"Authorization: Bearer <token>\""}

    @app.post("/api/claude-desktop")
    async def add_to_claude_desktop() -> dict[str, Any]:
        """Add FVS to Claude Desktop's config, keeping everything else in it."""
        path = claude_desktop_config_path()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raise FvsError(ErrorCode.SETTINGS_INVALID, f"{path} is not valid JSON, so "
                               "FVS did not change it.", "Fix or remove that file, then "
                               "try again, or copy the config by hand.") from None
            shutil.copy2(path, path.with_suffix(".json.bak"))
        data.setdefault("mcpServers", {})["fvs"] = desktop_entry()
        write_text_atomic(path, json.dumps(data, indent=2))
        core.activity.add("Added FVS to Claude Desktop. Restart Claude Desktop to use it.",
                          source="creator")
        return {"installed": True, "path": str(path)}

    @app.get("/api/token")
    async def token() -> dict[str, str]:
        """The full session token, for the Copy button (the caller already holds it)."""
        return {"token": core.token}

    # ---- folders, templates, assets ----------------------------------------------------

    @app.put("/api/folders")
    async def folders(data: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        out = str(data.get("output", "")).strip()
        if not out:
            raise FvsError(ErrorCode.SETTINGS_INVALID, "The output folder is empty.",
                           "Type the folder where approved videos should go.")
        core.config.folders.output = Path(out)
        save_config(core.config, core.home)
        core.activity.add(f"Output folder set to {out} (applies after a restart).",
                          source="creator")
        return {"output": out, "restart_needed": True}

    @app.get("/api/templates")
    async def templates() -> dict[str, Any]:
        return {"items": core.templates.list(), "caption_styles":
                core.templates.caption_styles()}

    @app.get("/api/assets")
    async def assets(kind: str | None = None) -> dict[str, Any]:
        return core.assets.list_assets(kind=kind)

    # ---- the web app itself ------------------------------------------------------------

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str, request: Request) -> Response:
        if path.startswith(("api/", "mcp")):
            raise FvsError(ErrorCode.NOT_FOUND, f"There is no /{path}.",
                           "See /api/openapi.json for the API.")
        file = (WEB_DIST / path).resolve()
        if path and file.is_file() and file.is_relative_to(WEB_DIST):
            return FileResponse(file, media_type=WEB_TYPES.get(file.suffix),
                                headers={"Cache-Control": "public, max-age=31536000, immutable"}
                                if file.parent.name == "assets" else None)
        index = WEB_DIST / "index.html"
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse("<!doctype html><title>Faceless Video Studio</title>"
                            "<p>The FVS core is running, but the web app is not built. "
                            "Run <code>npm run build</code> in <code>web/</code>.</p>")
