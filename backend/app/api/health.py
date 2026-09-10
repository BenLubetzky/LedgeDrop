"""Liveness and readiness endpoints.

``/health`` is a pure liveness check with no dependencies - it is the platform
health-check path, so a transient dependency blip does not cycle the instance.
``/health/ready`` additionally confirms the database and the storage backend are
reachable and returns ``503`` if either is down (Stage 9 Package 6).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_storage
from app.core.config import settings
from app.services.storage import FileStorage

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Check the backend/API process is running."""
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}


async def _probe_storage(storage: FileStorage) -> str:
    """Cheap reachability check for the configured storage backend."""
    try:
        health_check = getattr(storage, "health_check", None)
        if health_check is not None:  # S3-backed
            await health_check()
        elif settings.storage_backend == "local":
            settings.upload_directory.mkdir(parents=True, exist_ok=True)
        return "ok"
    except Exception:
        return "unavailable"


@router.get("/health/ready")
async def ready(
    db: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[FileStorage, Depends(get_storage)],
    response: Response,
) -> dict[str, str]:
    """Confirm the backend can reach the database and the storage backend."""
    checks = {"status": "ok", "database": "ok", "storage": "ok"}

    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        checks["database"] = "unavailable"

    checks["storage"] = await _probe_storage(storage)

    if checks["database"] != "ok" or checks["storage"] != "ok":
        checks["status"] = "degraded"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return checks
