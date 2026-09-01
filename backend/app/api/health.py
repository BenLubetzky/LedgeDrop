"""Liveness and readiness endpoints.

These exist so the foundation is runnable and verifiable before any document
routes are added. ``/health`` is a pure liveness check; ``/health/ready`` also
confirms the database is reachable.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import settings

router = APIRouter(tags=["health"])

@router.get("/health")
async def health() -> dict[str, str]:
    """Check the backend/API is running"""
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}


@router.get("/health/ready")
async def ready(db: Annotated[AsyncSession, Depends(get_db)]) -> dict[str, str]:
    """Check the backend can communicate with the database"""
    await db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
