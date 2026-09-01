"""Shared FastAPI dependencies.

``get_db`` is re-exported from :mod:`app.database.session` unchanged so route
modules have a single import site for dependencies. It must not be wrapped in
another async generator here - that would swallow its commit/rollback handling.
"""

from __future__ import annotations

from app.core.config import settings
from app.database.session import get_db
from app.services.storage import LocalFileStorage

__all__ = ["get_db", "get_storage"]

_storage = LocalFileStorage(settings.upload_directory)


def get_storage() -> LocalFileStorage:
    """Return the process-wide local file storage service."""
    return _storage
