"""The MCP server's contract: protocol, schemas, result and error shapes, security, the
activity feed, progress, and the stdio proxy with the official SDK client (MCP-1–11,
AC-9, AC-11)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import SecretStr

from fvs.api import create_app
from fvs.config import Config, save_config
from fvs.core import Core
from fvs.jobs import JobContext
from fvs.mcp.registry import MAX_RESULT_BYTES
from tests.conftest import add_clip

KEY = "sk_live_TESTKEY_0123456789wxyz"
BASE = "http://127.0.0.1:8787"
P1_NOW = {"fvs_guide", "fvs_library_scan", "fvs_project_create", "fvs_project_get",
          "fvs_clip_inspect", "fvs_frames", "fvs_storyboard", "fvs_listen", "fvs_script_save",
          "fvs_voiceover_generate", "fvs_edit_plan", "fvs_edit_save", "fvs_render",
          "fvs_job_get", "fvs_job_cancel", "fvs_settings"}


@pytest.fixture
def mcore(home: Path) -> Iterator[Core]:
    save_config(Config(elevenlabs_api_key=SecretStr(KEY)), home)
    c = Core.open(home)
    yield c
    c.jobs.stop(timeout=5)


@pytest.fixture
def client(mcore: Core) -> Iterator[TestClient]:
    with TestClient(create_app(mcore), base_url=BASE) as c:
        c.headers["Authorization"] = f"Bearer {mcore.token}"
        yield c


def rpc(client: TestClient, method: str, params: dict | None = None, id_: int = 1) -> dict:
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    r = client.post("/mcp", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def call(client: TestClient, name: str, arguments: dict | None = None) -> dict:
    out = rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "result" in out, out
    return out["result"]


def error_of(result: dict) -> dict:
    assert result["isError"] is True
    return json.loads(result["content"][0]["text"])


# ---- protocol ---------------------------------------------------------------------------


def test_initialize_and_notifications(client: TestClient) -> None:
    init = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "1"}})["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["capabilities"]["tools"] and init["serverInfo"]["name"] == "fvs"
    assert "fvs_guide" in init["instructions"]
    unknown = rpc(client, "initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert unknown["protocolVersion"] == "2025-11-25"
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""
    assert rpc(client, "ping")["result"] == {}
    assert rpc(client, "resources/list")["error"]["code"] == -32601
    assert client.get("/mcp").status_code == 405


def test_bad_json_is_a_parse_error(client: TestClient) -> None:
    r = client.post("/mcp", content=b"{nope", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700


def test_tool_definitions(client: TestClient) -> None:
    tools = rpc(client, "tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == P1_NOW
    for t in tools:
        Draft202012Validator.check_schema(t["inputSchema"])
        assert t["inputSchema"]["type"] == "object"
        assert len(t["description"]) > 60
        images = t["name"] in ("fvs_frames", "fvs_storyboard", "fvs_listen",
                               "fvs_clip_inspect")
        assert ("outputSchema" in t) is not images  # ADR-8
    by = {t["name"]: t for t in tools}
    assert by["fvs_frames"]["annotations"]["readOnlyHint"] is True  # MCP-7
    assert by["fvs_render"]["annotations"]["readOnlyHint"] is False
    voice = by["fvs_voiceover_generate"]
    assert "COSTS MONEY" in voice["description"] and voice["annotations"]["openWorldHint"]
    assert "allow_over_budget" in voice["inputSchema"]["properties"]


def test_prompts(client: TestClient) -> None:
    prompts = rpc(client, "prompts/list")["result"]["prompts"]
    assert [p["name"] for p in prompts] == ["process_folder", "revise_video", "review_queue",
                                           "resume_batch"]
    got = rpc(client, "prompts/get", {"name": "revise_video",
                                      "arguments": {"project": "3", "request": "faster"}})
    text = got["result"]["messages"][0]["content"]["text"]
    assert "#3" in text and "faster" in text
    assert rpc(client, "prompts/get", {"name": "revise_video"})["error"]["code"] == -32602


# ---- results and errors -----------------------------------------------------------------


def test_guide_result_shape_and_size(client: TestClient) -> None:
    started = time.monotonic()
    r = call(client, "fvs_guide")
    assert time.monotonic() - started < 20  # AC-11
    assert r["isError"] is False
    assert r["content"][0]["text"] == r["structuredContent"]["summary"]
    assert json.loads(r["content"][1]["text"]) == r["structuredContent"]
    data = r["structuredContent"]
    assert {"workflow", "markup", "edit_spec_schema", "templates", "caption_styles",
            "voices", "budget", "quota", "settings"} <= set(data)
    assert len(json.dumps(r)) < MAX_RESULT_BYTES
    only = call(client, "fvs_guide", {"sections": ["markup"]})["structuredContent"]
    assert set(only) == {"markup", "summary"}


def test_errors_have_code_message_fix(client: TestClient) -> None:
    err = error_of(call(client, "fvs_project_get", {"project": 99}))
    assert err["code"] == "NOT_FOUND" and err["fix"]
    bad = error_of(call(client, "fvs_project_get", {"project": 1, "colour": "red"}))
    assert bad["code"] == "SPEC_INVALID" and bad["example"] == '{"project": 3}'
    assert bad["details"][0]["path"] == "colour"
    missing = error_of(call(client, "fvs_frames", {}))
    assert missing["details"][0]["path"] == "source"
    out = rpc(client, "tools/call", {"name": "fvs_nope", "arguments": {}})
    assert out["error"]["code"] == -32602


def test_one_activity_line_per_call(client: TestClient, mcore: Core) -> None:
    def lines() -> list[dict]:
        with mcore.db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM activity ORDER BY id")]

    before = len(lines())
    call(client, "fvs_guide")
    after_guide = lines()
    assert len(after_guide) == before + 1 and after_guide[-1]["tool"] == "fvs_guide"
    n = add_clip(mcore)
    p = mcore.projects.create(clips=[n], source="creator")
    before = len(lines())
    r = call(client, "fvs_script_save", {"project": p["number"],
                                         "voiceover": "Volcanoes make lightning."})
    assert r["isError"] is False
    new = lines()[before:]
    assert len(new) == 1 and new[0]["tool"] == "fvs_script_save"  # the service's own line
    assert new[0]["project_id"] == p["id"]
    before = len(lines())
    call(client, "fvs_project_get", {"project": 77})
    failed = lines()[before:]
    assert len(failed) == 1 and "failed" in failed[0]["summary"]


def test_project_get_has_the_whole_state(client: TestClient, mcore: Core) -> None:
    n = add_clip(mcore)
    p = mcore.projects.create(clips=[n], source="creator")
    mcore.scripts.save(p["number"], voiceover="Volcanoes make lightning.", source="creator")
    data = call(client, "fvs_project_get", {"project": f"#{p['number']}"})["structuredContent"]
    assert data["project"]["state"] == "scripted" and data["state_label"]
    assert data["script"]["sentences"][0]["i"] == 1
    assert data["renders"] == [] and data["reviews"] == [] and data["history"]
    assert data["clips"][0]["number"] == n


def test_settings_tool_never_shows_the_key(client: TestClient, mcore: Core) -> None:
    changed = call(client, "fvs_settings", {"changes": {"video.max_seconds": 75}})
    assert changed["structuredContent"]["settings"]["video.max_seconds"] == 75
    bad = error_of(call(client, "fvs_settings", {"changes": {"video.max_seconds": -1}}))
    assert bad["code"] == "SETTINGS_INVALID"
    for name in ("fvs_settings", "fvs_guide"):
        assert KEY not in json.dumps(call(client, name))
        assert KEY[-4:] not in json.dumps(call(client, name)["structuredContent"]
                                          .get("settings", {}))
    log_dir = mcore.config.folders.expanded().workspace / "logs"
    for f in log_dir.glob("*"):
        assert KEY not in f.read_text(encoding="utf-8", errors="ignore")


# ---- security (MCP-10) ------------------------------------------------------------------


def test_mcp_security(mcore: Core) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    with TestClient(create_app(mcore), base_url=BASE) as c:
        assert c.post("/mcp", json=body).status_code == 401
        auth = {"Authorization": f"Bearer {mcore.token}"}
        assert c.post("/mcp", json=body, headers={**auth, "Host": "evil.test:8787"}) \
            .status_code == 403
        assert c.post("/mcp", json=body, headers={**auth, "Origin": "https://evil.test"}) \
            .status_code == 403
        assert c.post("/mcp", content=json.dumps(body), headers={
            **auth, "Content-Type": "text/plain"}).status_code == 415
        assert c.post("/mcp", json=body, headers=auth).status_code == 200


def test_paths_outside_the_folders_are_refused(client: TestClient) -> None:
    err = error_of(call(client, "fvs_library_scan", {"folder": "C:/Windows"}))
    assert err["code"] == "PATH_NOT_ALLOWED"


# ---- jobs -------------------------------------------------------------------------------


def _slow_job(ctx: JobContext) -> dict:
    for k in range(1, 4):
        time.sleep(0.15)
        ctx.progress(k / 4, f"step {k}")
    return {"summary": "done slowly"}


def test_job_get_waits_for_a_change(client: TestClient, mcore: Core) -> None:
    mcore.jobs.register("slow", _slow_job)
    job = mcore.jobs.submit("slow", {})
    started = time.monotonic()
    data = call(client, "fvs_job_get", {"jobs": [job["id"]], "wait_s": 10})
    assert time.monotonic() - started < 10
    assert data["structuredContent"]["jobs"][0]["state"] in ("running", "done")
    while data["structuredContent"]["jobs"][0]["state"] != "done":
        data = call(client, "fvs_job_get", {"jobs": [job["id"]], "wait_s": 10})
    assert data["structuredContent"]["jobs"][0]["result"]["summary"] == "done slowly"
    assert call(client, "fvs_job_get", {"wait_s": 0})["structuredContent"]["summary"] == \
        "No active jobs."


def test_job_get_streams_progress_when_asked(client: TestClient, mcore: Core) -> None:
    mcore.jobs.register("slow", _slow_job)
    job = mcore.jobs.submit("slow", {})
    body = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {
        "name": "fvs_job_get", "arguments": {"jobs": [job["id"]], "wait_s": 10},
        "_meta": {"progressToken": "p1"}}}
    events = []
    with client.stream("POST", "/mcp", json=body,
                       headers={"Accept": "application/json, text/event-stream"}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]
    final = events[-1]
    assert final["id"] == 9 and "result" in final
    progress = [e for e in events if e.get("method") == "notifications/progress"]
    assert all(e["params"]["progressToken"] == "p1" for e in progress)


# ---- the stdio proxy with the official SDK client ---------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(home: Path, tmp_path: Path) -> Iterator[Core]:
    port = _free_port()
    save_config(Config(port=port), home)
    core = Core.open(home)
    server = uvicorn.Server(uvicorn.Config(create_app(core), host="127.0.0.1", port=port,
                                           log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield core
    server.should_exit = True
    thread.join(timeout=10)


def test_stdio_proxy_with_the_sdk_client(served: Core, home: Path, tmp_path: Path) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "FVS_HOME": str(home), "FVS_ROOT": str(tmp_path / "FVS")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "fvs", "mcp"],
                                   env=env)

    async def session() -> tuple[set[str], dict, list[str]]:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            guide = await s.call_tool("fvs_guide", {"sections": ["voices"]})
            prompts = [p.name for p in (await s.list_prompts()).prompts]
            return tools, guide.structured_content, prompts

    tools, guide, prompts = asyncio.run(session())
    assert tools == P1_NOW
    assert guide["voices"][0]["alias"] == "narrator"
    assert len(prompts) == 4
