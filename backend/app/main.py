"""FastAPI application entry point.

Run in development with::

    uv run uvicorn app.main:app --reload

``create_app`` is a factory so tests can build an isolated instance.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.database.session import dispose_engine

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Define what happens when FastAPI server starts and when it shuts down"""
    # Before the server startup make sure upload directory exists
    settings.upload_directory.mkdir(parents=True, exist_ok=True)
    yield
    # After the server shutdown dispose engine
    await dispose_engine()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Create a FastAPI instance
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        summary="Business document processing - Stage 2: upload foundation",
        lifespan=lifespan,
    )

    # Adding CORS middleware to allow frontends from different origins to interact 
    # with API
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
