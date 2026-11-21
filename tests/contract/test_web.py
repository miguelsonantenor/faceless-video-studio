"""The web app's routes (REV-1–5, BUILD_PLAN WP8): overview, projects, restore, the key
(never read back), connecting Claude Desktop, folders, and serving the built app."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from fvs.api import create_app, web
from fvs.core import Core
from tests.conftest import add_clip, make_project, voiced_project
from tests.fake_elevenlabs import FakeElevenLabs

BASE = "http://127.0.0.1:8787"
NEW_KEY = "sk_new_TESTKEY_abcdefghij9876"


@pytest.fixture
def client(core: Core) -> Iterator[TestClient]:
    core.voice.client = FakeElevenLabs().client()
    with TestClient(create_app(core), base_url=BASE) as c:
        c.headers["Authorization"] = f"Bearer {core.token}"
        yield c


def test_overview_counts_every_state_and_sees_claude(client: TestClient, core: Core) -> None:
    make_project(core, "Ants")
    data = client.get("/api/overview").json()
    assert set(data["counts"]) == set(data["labels"]) and data["counts"]["new"] == 1
    assert data["claude_connected"] is False
    assert data["budget"]["limit"] == 20_000 and data["quota"]["available"] is True
    assert data["activity"] and data["jobs"] == []
    client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert client.get("/api/overview").json()["claude_connected"] is True


def test_projects_list_says_where_each_stands(client: TestClient, core: Core) -> None:
    make_project(core, "Ants")
    voiced_project(core, [add_clip(core)])
    items = {i["title"]: i for i in client.get("/api/projects").json()["items"]}
    assert items["Ants"]["where"] == "New" and items["Ants"]["clips"]
    assert items["Volcano"]["where"].startswith("Script v1, 3 sentence(s)")
    assert items["Volcano"]["length"] is None and items["Volcano"]["qa"] is None
    only = client.get("/api/projects", params={"state": "voiced"}).json()["items"]
    assert [i["title"] for i in only] == ["Volcano"]


def test_where_it_stands_for_qa_and_notes() -> None:
    row = {"failed": 0, "last_error": None, "state": "in_review"}
    qa = {"status": "fail", "checks": [{"name": "Loudness", "status": "fail"},
                                       {"name": "Format", "status": "pass"}]}
    assert web.where_it_stands(row, job=None, qa=qa, note=None, script=None) == \
        "1 check(s) failed: loudness"
    assert web.where_it_stands(row, job=None, qa={"status": "pass", "checks": []},
                               note=None, script=None) == "All 8 checks passed"
    assert web.where_it_stands({**row, "state": "changes_requested"}, job=None, qa=None,
                               note="hook too slow", script=None) == "Note: hook too slow"
    assert web.where_it_stands(row, job={"kind": "render", "state": "running",
                                         "progress": 0.62}, qa=None, note=None,
                               script=None) == "Render 62%"
    failed = {**row, "failed": 1, "last_error": json.dumps({"message": "Disk was full"})}
    assert web.where_it_stands(failed, job=None, qa=None, note=None, script=None) == \
        "Disk was full"


def test_project_detail_and_restore(client: TestClient, core: Core) -> None:
    p = voiced_project(core, [add_clip(core)])
    core.scripts.save(p["number"], voiceover="A shorter script.", hook="Short")
    data = client.get(f"/api/projects/{p['number']}").json()
    assert data["project"]["script_version"] == 2 and data["script"]["hook"] == "Short"
    assert [s["version"] for s in data["scripts"]] == [1, 2]
    assert data["scripts"][0]["voiceover_text"].startswith("The volcano erupts.")
    assert data["voiceover"]["voice_alias"] == "narrator" and data["specs"] == []
    r = client.post(f"/api/projects/{p['number']}/restore", json={"script_version": 1})
    assert r.status_code == 200, r.text
    assert core.projects.get_row(p["number"])["script_v"] == 1
    assert client.get(f"/api/projects/{p['number']}/thumbs/0.jpg").status_code == 404
    assert client.get("/api/projects/99").status_code == 404


def test_review_from_the_web_needs_a_note_to_reject(client: TestClient, core: Core) -> None:
    p = make_project(core)
    r = client.post("/api/review", json={"projects": [p["number"]], "decision": "reject"})
    assert r.status_code == 422 and "note" in r.json()["message"]
    r = client.post("/api/review", json={"projects": [p["number"]], "decision": "skip"})
    assert r.status_code == 200 and core.projects.get_row(p["number"])["state"] == "skipped"
    assert client.post("/api/review/approve-passed").json()["items"] == []


def test_key_is_tested_saved_and_never_read_back(client: TestClient, core: Core,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.put("/api/key", json={"key": "short"}).status_code == 422
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(401))
    assert client.put("/api/key", json={"key": NEW_KEY}).json()["code"] == "VOICE_AUTH"
    assert core.config.elevenlabs_api_key is None

    seen: list[str] = []

    def ok(url: str, *, headers: dict, timeout: float) -> httpx.Response:
        seen.append(headers["xi-api-key"])
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx, "get", ok)
    out = client.put("/api/key", json={"key": NEW_KEY}).json()
    assert seen == [NEW_KEY] and out["key_last4"] == "9876"
    assert NEW_KEY in (core.home / "config.toml").read_text(encoding="utf-8")
    for path in ("/api/settings", "/api/overview", "/api/activity"):
        assert NEW_KEY not in client.get(path).text


def test_claude_desktop_config_is_merged_with_a_backup(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setattr(web, "claude_desktop_config_path", lambda: path)
    info = client.get("/api/claude-desktop").json()
    assert info["installed"] is False and info["mcp_address"] == f"{BASE}/mcp"
    assert client.post("/api/claude-desktop").json()["installed"] is True  # creates the file

    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}),
                    encoding="utf-8")
    client.post("/api/claude-desktop")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {"other", "fvs"} and data["theme"] == "dark"
    assert json.loads(path.with_suffix(".json.bak").read_text())["mcpServers"] == \
        {"other": {"command": "x"}}
    assert client.get("/api/claude-desktop").json()["installed"] is True

    path.write_text("{not json", encoding="utf-8")
    r = client.post("/api/claude-desktop")
    assert r.status_code == 422 and path.read_text(encoding="utf-8") == "{not json"


def test_token_folders_templates_assets(client: TestClient, core: Core,
                                        tmp_path: Path) -> None:
    assert client.get("/api/token").json()["token"] == core.token
    assert client.put("/api/folders", json={"output": " "}).status_code == 422
    out = tmp_path / "exports"
    assert client.put("/api/folders", json={"output": str(out)}).json()["restart_needed"]
    assert str(out).replace("\\", "\\\\") in (core.home / "config.toml").read_text(
        encoding="utf-8")
    templates = client.get("/api/templates").json()
    assert templates["items"] and templates["caption_styles"]
    assert client.get("/api/assets", params={"kind": "music"}).json()["items"] == []
    assert client.get("/api/jobs/nope/log").status_code == 404


def test_web_app_is_served_with_its_routes(client: TestClient) -> None:
    index = client.get("/")
    assert index.status_code == 200 and '<div id="root">' in index.text
    assert client.get("/projects/3").text == index.text  # client-side routes
    assert client.get("/review").text == index.text
    asset = next((web.WEB_DIST / "assets").glob("*.js")).name
    assert client.get(f"/assets/{asset}").headers["content-type"].startswith(
        "text/javascript")
    assert client.get("/api/nope").status_code == 404
    assert client.get("/..%2fpyproject.toml").text == index.text  # never outside the app


def test_web_app_needs_the_session(core: Core) -> None:
    with TestClient(create_app(core), base_url=BASE) as anon:
        assert anon.get("/").status_code == 401
        signed = anon.get(f"/auth?t={core.token}")
        assert signed.status_code == 200 and '<div id="root">' in signed.text


def test_a_browser_without_the_session_gets_a_readable_page(core: Core) -> None:
    with TestClient(create_app(core), base_url=BASE) as anon:
        page = anon.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
        assert page.status_code == 401 and page.headers["content-type"].startswith("text/html")
        assert "fvs open" in page.text and core.token not in page.text
        api = anon.get("/api/projects", headers={"Accept": "text/html"})
        assert api.status_code == 401 and api.json()["code"] == "UNAUTHORIZED"
