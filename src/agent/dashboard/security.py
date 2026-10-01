"""
Dashboard request guard — two layers, both in one ASGI middleware.

1. Cross-site guard (always on). The server listens on 127.0.0.1, but the
   browser that talks to it also visits other websites, and those pages can
   make that browser POST to localhost. Any state-changing request (non-GET)
   or WebSocket whose Origin isn't this dashboard is refused. Same-origin
   use, curl and the CLI (no Origin header) are unaffected. This replaces
   the old CORS allow_origins=["*"].

2. Login (opt-in). Set DASHBOARD_TOKEN (e.g. `agent secrets set
   DASHBOARD_TOKEN --generate`) and every /api and /ws request needs it —
   as the HttpOnly session cookie set by /api/login, or an
   `Authorization: Bearer` header. Do this before exposing the dashboard
   beyond your own machine (ngrok, a server, a shared network).
"""
from __future__ import annotations

import hmac
import json
import os
from urllib.parse import urlsplit

COOKIE = "atlas_session"
PUBLIC_PATHS = {"/api/login", "/api/logout", "/api/auth/status"}
_DEV_ORIGINS = {"localhost:5173", "127.0.0.1:5173"}   # vite dev server proxy


def expected_token() -> str:
    return os.environ.get("DASHBOARD_TOKEN", "").strip()


def token_ok(presented: str) -> bool:
    token = expected_token()
    return bool(token) and bool(presented) and hmac.compare_digest(presented.encode(), token.encode())


def _extra_origins() -> set[str]:
    raw = os.environ.get("DASHBOARD_ALLOWED_ORIGINS", "")
    return {urlsplit(o.strip()).netloc for o in raw.split(",") if o.strip()}


def origin_allowed(origin: str, host: str) -> bool:
    """Same-origin (Origin's host:port == the Host header), the vite dev
    server, or an explicitly allowed origin."""
    netloc = urlsplit(origin).netloc.lower()
    if not netloc:
        return False       # "null" origins (sandboxed iframes, file://) are cross-site
    return netloc == host.lower() or netloc in _DEV_ORIGINS or netloc in _extra_origins()


def _cookie(headers: dict[str, str]) -> str:
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return ""


def _bearer(headers: dict[str, str]) -> str:
    auth = headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


class GuardMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        is_ws = scope["type"] == "websocket"
        unsafe = is_ws or scope.get("method", "GET") not in ("GET", "HEAD", "OPTIONS")

        origin = headers.get("origin")
        if unsafe and origin and not origin_allowed(origin, headers.get("host", "")):
            return await self._deny(scope, receive, send, is_ws, 403,
                                    f"Cross-site request from {origin} blocked.")

        if expected_token() and (path.startswith("/api/") or path.startswith("/ws/")) \
                and path not in PUBLIC_PATHS:
            if not (token_ok(_cookie(headers)) or token_ok(_bearer(headers))):
                return await self._deny(scope, receive, send, is_ws, 401, "Login required.")

        return await self.app(scope, receive, send)

    @staticmethod
    async def _deny(scope, receive, send, is_ws: bool, status: int, message: str):
        if is_ws:
            await receive()   # the websocket.connect event
            await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403})
            return
        body = json.dumps({"detail": message}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
