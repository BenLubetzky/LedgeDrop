"""Top-level API router.

Document routes (``POST /documents`` and friends) will be registered here in the
next step of Stage 2.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health

api_router = APIRouter()
api_router.include_router(health.router)
