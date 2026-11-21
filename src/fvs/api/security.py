"""Request checks for everything the core serves: ``/api``, the web app and ``/mcp``.

In this order (MCP-10, NFR-S1, DS 10.1):
1. ``Host`` must be ``127.0.0.1:<port>`` or ``localhost:<port>``, else 403. This blocks
   DNS-rebinding attacks from web pages.
2. An ``Origin`` header, when present, must be the web app's own, else 403.
3. The session token is required, as ``Authorization: Bearer <token>`` or the
   ``fvs_session`` cookie, else 401. Only ``/auth`` (which sets the cookie) is open.
4. Request bodies (POST, PUT, PATCH) must be ``application/json``, else 415.

Written as plain ASGI middleware so it also wraps streaming responses and mounted apps.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterable
from http.cookies import CookieError, SimpleCookie
from typing import Any

COOKIE_NAME = "fvs_session"
OPEN_PATHS = frozenset({"/auth"})
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


def _headers(scope: dict[str, Any]) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}


def _cookie(headers: dict[str, str], name: str) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


def token_matches(presented: str | None, token: str) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(presented.encode(), token.encode())


class SecurityMiddleware:
    def __init__(self, app: Any, *, token: str, port: int,
                 extra_origins: Iterable[str] = ()) -> None:
        self.app = app
        self.token = token
        self.allowed_hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
        self.allowed_origins = frozenset(
            {f"http://127.0.0.1:{port}", f"http://localhost:{port}",
             *(o.rstrip("/").lower() for o in extra_origins)}
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":  # FVS serves no websockets; refuse them all
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":  # lifespan
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)

        if headers.get("host", "").lower() not in self.allowed_hosts:
            await self._reply(send, 403, "FORBIDDEN_HOST",
                              "Requests must be addressed to 127.0.0.1 or localhost.",
                              "Use http://127.0.0.1:<port>/ on this computer.")
            return
        origin = headers.get("origin")
        if origin is not None and origin.rstrip("/").lower() not in self.allowed_origins:
            await self._reply(send, 403, "FORBIDDEN_ORIGIN",
                              "Requests from other web pages are refused.",
                              "Open the FVS web app itself, or call from a local program "
                              "without an Origin header.")
            return
        if scope["path"] not in OPEN_PATHS and not self._authorised(headers):
            if _is_page_visit(scope, headers):
                await _send(send, 401, "text/html; charset=utf-8", SIGN_IN_PAGE)
                return
            await self._reply(send, 401, "UNAUTHORIZED",
                              "The FVS session token is missing or wrong.",
                              "Send 'Authorization: Bearer <token>' with the token from "
                              "~/.fvs/token, or open the web app with `fvs open`.")
            return
        has_body = headers.get("content-length", "0") != "0" or "transfer-encoding" in headers
        if scope["method"] in BODY_METHODS and has_body:
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                await self._reply(send, 415, "UNSUPPORTED_MEDIA_TYPE",
                                  "Only JSON request bodies are accepted.",
                                  "Send 'Content-Type: application/json'.")
                return
        await self.app(scope, receive, send)

    def _authorised(self, headers: dict[str, str]) -> bool:
        auth = headers.get("authorization", "")
        if auth[:7].lower() == "bearer " and token_matches(auth[7:].strip(), self.token):
            return True
        return token_matches(_cookie(headers, COOKIE_NAME), self.token)

    @staticmethod
    async def _reply(send: Any, status: int, code: str, message: str, fix: str) -> None:
        body = json.dumps({"code": code, "message": message, "fix": fix}).encode()
        await _send(send, status, "application/json", body)


def _is_page_visit(scope: dict[str, Any], headers: dict[str, str]) -> bool:
    """A browser opening a page (not the API, MCP or a script fetching data)."""
    return (scope["method"] == "GET" and "text/html" in headers.get("accept", "")
            and not scope["path"].startswith(("/api/", "/mcp")))


async def _send(send: Any, status: int, content_type: str, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", content_type.encode()),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


# Shown to a browser without the session cookie. It never contains the token.
SIGN_IN_PAGE = b"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Faceless Video Studio</title><style>
body{margin:0;background:#F6F6F3;color:#17181C;font:15px/1.5 system-ui,sans-serif}
main{max-width:560px;margin:12vh auto;padding:28px 32px;background:#fff;
border:1px solid #E3E3DE;border-radius:12px}h1{font-size:22px;margin:0 0 8px}
code{background:#F3F3EF;border:1px solid #E3E3DE;border-radius:6px;padding:2px 6px}
p{color:#3D4049}</style></head><body><main>
<h1>Faceless Video Studio is running</h1>
<p>This browser is not signed in. To open the web app, run this in a terminal:</p>
<p><code>fvs open</code></p>
<p>It opens this page signed in. Only programs on this computer with the session token
can use FVS.</p></main></body></html>"""
