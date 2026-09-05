"""Top-level API router."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    documents,
    extractions,
    health,
    normalizations,
    pipeline,
    validations,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(documents.router)
api_router.include_router(extractions.router)
api_router.include_router(normalizations.router)
api_router.include_router(validations.router)
api_router.include_router(pipeline.router)
