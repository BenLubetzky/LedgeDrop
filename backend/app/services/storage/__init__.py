"""Original-PDF storage: a common interface with a filesystem and an S3 backend.

``build_storage(settings)`` returns the backend named by ``STORAGE_BACKEND``
(``local`` for development/test, ``s3`` for deployed environments). The S3
backend and its ``aioboto3`` dependency are imported lazily so a local run
never loads them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.storage.base import FileStorage, StorageError, StoredFile
from app.services.storage.local import LocalFileStorage

if TYPE_CHECKING:  # pragma: no cover
    from app.core.config import Settings

__all__ = [
    "FileStorage",
    "LocalFileStorage",
    "StorageError",
    "StoredFile",
    "build_storage",
]


def build_storage(settings: "Settings") -> FileStorage:
    """Construct the storage backend selected by ``settings.storage_backend``."""
    if settings.storage_backend == "s3":
        from app.services.storage.s3 import S3FileStorage

        return S3FileStorage(
            bucket=settings.s3_bucket or "",
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            access_key_id=(
                settings.s3_access_key_id.get_secret_value()
                if settings.s3_access_key_id is not None
                else None
            ),
            secret_access_key=(
                settings.s3_secret_access_key.get_secret_value()
                if settings.s3_secret_access_key is not None
                else None
            ),
            key_prefix=settings.s3_key_prefix,
        )
    return LocalFileStorage(settings.upload_directory)
