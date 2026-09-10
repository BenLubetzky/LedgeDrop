"""IP-keyed request rate limiting (Stage 9 Package 5), backed by slowapi.

Disabled by default (development/tests); enabled with ``RATE_LIMIT_ENABLED`` in
deployed environments. ``RATE_LIMIT_DEFAULT`` applies to every route via the
middleware; ``RATE_LIMIT_UPLOAD`` is an extra, stricter limit on
``POST /documents`` (applied with ``@limiter.limit`` on that route).

The in-memory store is fine for the single-instance MVP; horizontal scaling
would need a shared store (see the runbook).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from app.core.config import settings

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.rate_limit_default] if settings.rate_limit_enabled else [],
    enabled=settings.rate_limit_enabled,
    headers_enabled=True,
    storage_uri="memory://",
)

UPLOAD_RATE_LIMIT = settings.rate_limit_upload


async def _rate_limit_exceeded(_: Request, __: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "code": "RATE_LIMITED",
                "message": "Too many requests. Please retry shortly.",
            }
        },
    )


def install_rate_limiting(app: FastAPI) -> None:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded)
    app.add_middleware(SlowAPIMiddleware)
