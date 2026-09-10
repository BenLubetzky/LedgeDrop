"""Stage 9 edge-hardening middleware (Package 5) and request correlation.

All three are pure-ASGI middleware so a context variable set here propagates to
the endpoint (``BaseHTTPMiddleware`` runs the downstream in a separate task and
would not).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.core.request_context import bind_request_id, reset_request_id

_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")

_STATIC_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
)
_API_CSP = b"default-src 'none'; frame-ancestors 'none'"
_HSTS = b"max-age=31536000; includeSubDomains"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DOCUMENT_FILE_PATH = re.compile(r"^/documents/[^/]+/file$")


class RequestIDMiddleware:
    """Assign / propagate an ``X-Request-ID`` and bind it for logging."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = (Headers(scope=scope).get("x-request-id") or "").strip()
        # Do not reflect arbitrary client bytes into a response header or logs.
        request_id = inbound if _VALID_REQUEST_ID.fullmatch(inbound) else uuid.uuid4().hex
        token = bind_request_id(request_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("x-request-id", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            reset_request_id(token)


class SecurityHeadersMiddleware:
    """Add conservative security headers to every response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        deployed = settings.environment in {"staging", "production"}
        document_file = bool(_DOCUMENT_FILE_PATH.fullmatch(path))

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _STATIC_SECURITY_HEADERS:
                    headers.setdefault(name.decode(), value.decode())
                if deployed:
                    headers.setdefault("strict-transport-security", _HSTS.decode())
                if document_file:
                    # The review editor intentionally embeds this PDF. X-Frame-
                    # Options cannot express a cross-origin allowlist, so use
                    # CSP and permit only configured LedgerDrop frontends.
                    allowed = " ".join(settings.cors_allow_origins)
                    headers.setdefault(
                        "content-security-policy", f"frame-ancestors {allowed}"
                    )
                elif not path.startswith(_DOC_PATHS):
                    headers.setdefault("x-frame-options", "DENY")
                    headers.setdefault("content-security-policy", _API_CSP.decode())
            await send(message)

        await self.app(scope, receive, send_wrapper)


class MaxBodySizeMiddleware:
    """Reject a request whose declared ``Content-Length`` exceeds ``max_bytes``.

    A coarse guard in front of the route-level streaming limit (which counts the
    bytes actually received) and the platform edge cap. Requests without a
    ``Content-Length`` fall through to the route-level check.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            declared = _content_length(scope.get("headers") or [])
            if declared is not None and declared > self.max_bytes:
                await _reject_too_large(send)
                return
        await self.app(scope, receive, send)


def _content_length(raw_headers: Iterable[tuple[bytes, bytes]]) -> int | None:
    for name, value in raw_headers:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _reject_too_large(send: Send) -> None:
    body = json.dumps(
        {"error": {"code": "REQUEST_TOO_LARGE", "message": "The request body is too large."}}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
