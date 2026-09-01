"""Top-level API router."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import documents, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(documents.router)
