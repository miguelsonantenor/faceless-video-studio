"""AC-9 (WP0 part) and NFR-S1/S2: token, Host, Origin, JSON-only, key never returned or logged."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fvs.api import create_app
from fvs.config import Config, save_config
from fvs.core import Core
from fvs.logs import get_logger

KEY = "sk_live_TESTKEY_0123456789wxyz"
BASE = "http://127.0.0.1:8787"


@pytest.fixture
def keyed_core(home: Path) -> Iterator[Core]:
    save_config(Config(elevenlabs_api_key=SecretStr(KEY)), home)
    c = Core.open(home)
    yield c
    c.jobs.stop(timeout=5)


@pytest.fixture
def client(keyed_core: Core) -> Iterator[TestClient]:
    with TestClient(create_app(keyed_core), base_url=BASE) as c:
        yield c


def auth(core: Core) -> dict[str, str]:
    return {"Authorization": f"Bearer {core.token}"}


def test_no_token_is_401(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED" and "fvs open" in r.json()["fix"]


def test_wrong_token_is_401(client: TestClient) -> None:
    assert client.get("/api/health", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_token_is_200(client: TestClient, keyed_core: Core) -> None:
    r = client.get("/api/health", headers=auth(keyed_core))
    assert r.status_code == 200 and r.json()["ok"] is True


def test_foreign_host_is_403_even_with_token(client: TestClient, keyed_core: Core) -> None:
    r = client.get("/api/health", headers={**auth(keyed_core), "Host": "evil.example:8787"})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_HOST"


def test_foreign_origin_is_403_even_with_token(client: TestClient, keyed_core: Core) -> None:
    r = client.get("/api/health", headers={**auth(keyed_core), "Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_ORIGIN"


def test_own_origin_is_fine(client: TestClient, keyed_core: Core) -> None:
    r = client.get("/api/health", headers={**auth(keyed_core), "Origin": BASE})
    assert r.status_code == 200


def test_mcp_path_is_guarded_too(client: TestClient) -> None:
    # /mcp itself arrives in WP6, but the guard already covers every path.
    assert client.post("/mcp", json={}).status_code == 401


def test_non_json_body_is_415(client: TestClient, keyed_core: Core) -> None:
    r = client.patch("/api/settings", content="x=1",
                     headers={**auth(keyed_core), "Content-Type": "text/plain"})
    assert r.status_code == 415


def test_auth_link_sets_session_cookie(client: TestClient, keyed_core: Core) -> None:
    assert client.get("/auth", params={"t": "wrong"}).status_code == 401
    r = client.get("/auth", params={"t": keyed_core.token}, follow_redirects=False)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/health").status_code == 200  # cookie now in the client jar


def test_api_key_is_never_returned(client: TestClient, keyed_core: Core) -> None:
    r = client.get("/api/settings", headers=auth(keyed_core))
    assert r.status_code == 200
    assert KEY not in r.text
    assert r.json()["elevenlabs"] == {"configured": True, "key_last4": "wxyz"}
    assert keyed_core.token not in r.text


def test_settings_patch(client: TestClient, keyed_core: Core) -> None:
    r = client.patch("/api/settings", json={"video.max_seconds": 90}, headers=auth(keyed_core))
    assert r.status_code == 200 and r.json()["settings"]["video.max_seconds"] == 90
    bad = client.patch("/api/settings", json={"video.target_min_seconds": 99},
                       headers=auth(keyed_core))
    assert bad.status_code == 422
    assert bad.json()["code"] == "SETTINGS_INVALID" and bad.json()["details"]


def test_api_key_and_token_never_reach_the_log(keyed_core: Core) -> None:
    log = get_logger("test")
    log.info("calling elevenlabs", detail=f"header xi-api-key: {KEY}", api_key=KEY)
    logging.getLogger("httpx").warning("GET with key %s and token %s", KEY, keyed_core.token)
    for h in logging.getLogger().handlers:
        h.flush()
    text = (keyed_core.config.folders.expanded().workspace / "logs" / "fvs.log").read_text(
        encoding="utf-8")
    assert "calling elevenlabs" in text
    assert KEY not in text and keyed_core.token not in text
    assert "***" in text


def test_doctor_endpoint(client: TestClient, keyed_core: Core) -> None:
    r = client.get("/api/doctor", headers=auth(keyed_core))
    assert r.status_code == 200
    names = {c["name"] for c in r.json()["checks"]}
    assert {"FFmpeg", "Disk space", "ElevenLabs key", "Claude Desktop"} <= names


def test_jobs_endpoints(client: TestClient, keyed_core: Core) -> None:
    job = keyed_core.jobs.submit("nothing-handles-this")
    r = client.get(f"/api/jobs/{job['id']}", headers=auth(keyed_core))
    assert r.status_code == 200 and r.json()["state"] == "queued"
    r = client.post(f"/api/jobs/{job['id']}/cancel", headers=auth(keyed_core))
    assert r.json()["state"] == "cancelled"
    assert client.get("/api/jobs/missing", headers=auth(keyed_core)).status_code == 404
