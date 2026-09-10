"""FastAPI application entry point.

Run in development with::

    uv run uvicorn app.main:app --reload

``create_app`` is a factory so tests can build an isolated instance.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.router import api_router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.middleware import (
    MaxBodySizeMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.observability import configure_logging, configure_sentry
from app.core.rate_limit import install_rate_limiting
from app.database.session import dispose_engine

configure_logging()
configure_sentry()

# Coarse guard ahead of the route-level streaming limit and the platform edge
# cap: reject a request whose declared body is well over the upload ceiling.
_MAX_REQUEST_BODY_BYTES = settings.max_file_size_bytes + 5 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Define what happens when FastAPI server starts and when it shuts down"""
    if settings.storage_backend == "local":
        settings.upload_directory.mkdir(parents=True, exist_ok=True)
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Interactive API docs are disabled in production (Stage 9 Package 5).
    docs_enabled = settings.environment != "production"

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        summary="Business document processing",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    # Rate limiting first so its middleware ends up innermost.
    install_rate_limiting(app)

    # add_middleware wraps outermost-last: request-id ends up outermost so every
    # downstream layer and every log line carries the correlation id.
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=_MAX_REQUEST_BODY_BYTES)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestIDMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router)

    # Standard request rate / latency / error metrics at /metrics. Skipped under
    # `test`, where the suite builds many app instances and re-registering the
    # collectors on the global Prometheus registry would raise.
    if not settings.is_test:
        Instrumentator(
            excluded_handlers=["/health", "/health/ready", "/metrics"]
        ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    return app


app = create_app()
