"""S3-compatible object storage for original uploaded PDFs.

Used in deployed environments with Cloudflare R2 (``STORAGE_BACKEND=s3``). The
object key is the storage-root-relative location
(``<document_id>/original.pdf``), optionally under ``S3_KEY_PREFIX`` - identical
in shape to :class:`~app.services.storage.local.LocalFileStorage`, so a
``documents`` row written before a cutover still resolves afterwards.

``aioboto3`` is imported here only; the module is imported lazily by
``build_storage`` so local/test runs never load it.
"""

from __future__ import annotations

import uuid
from pathlib import PurePosixPath

import aioboto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.services.storage.base import FileStorage, StorageError, StoredFile
from app.services.storage.local import ORIGINAL_FILENAME

_BOTO_CONFIG = BotoConfig(
    retries={"max_attempts": 3, "mode": "standard"},
    s3={"addressing_style": "path"},  # R2 and MinIO want path-style addressing
)

_NOT_FOUND_CODES = {"404", "NotFound", "NoSuchKey", "NoSuchBucket"}


def _is_not_found(exc: ClientError) -> bool:
    response = getattr(exc, "response", None) or {}
    code = response.get("Error", {}).get("Code", "")
    http_status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in _NOT_FOUND_CODES or http_status == 404


class S3FileStorage(FileStorage):
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None,
        region: str = "auto",
        access_key_id: str | None,
        secret_access_key: str | None,
        key_prefix: str = "",
    ) -> None:
        self._bucket = bucket
        self._endpoint_url = endpoint_url
        self._region = region or "auto"
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._prefix = key_prefix.strip("/")
        self._session = aioboto3.Session()

    def _client(self):
        return self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            region_name=self._region,
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
            config=_BOTO_CONFIG,
        )

    # -- path / key handling ------------------------------------------------

    def location_for(self, document_id: uuid.UUID | str) -> str:
        return str(PurePosixPath(str(document_id)) / ORIGINAL_FILENAME)

    def _key_for(self, location: str) -> str:
        """Map a stored location to an object key, rejecting anything unsafe.

        Mirrors ``LocalFileStorage.resolve``: no absolute path, no drive, no
        backslashes, no ``..`` segment.
        """
        if "\\" in location or ":" in location:
            raise StorageError("Storage location must be a POSIX relative path.")
        candidate = PurePosixPath(location)
        if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
            raise StorageError("Storage location must be a relative path without '..'.")
        parts = candidate.parts
        return "/".join((self._prefix, *parts)) if self._prefix else "/".join(parts)

    # -- operations -------------------------------------------------------

    async def save_bytes(self, document_id: uuid.UUID | str, content: bytes) -> StoredFile:
        location = self.location_for(document_id)
        key = self._key_for(location)
        try:
            async with self._client() as s3:
                await s3.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=content,
                    ContentType="application/pdf",
                )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"Failed to store document {document_id}.") from exc
        return StoredFile(location=location, path=None, size_bytes=len(content))

    async def exists(self, location: str) -> bool:
        key = self._key_for(location)
        try:
            async with self._client() as s3:
                await s3.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise StorageError("Failed to check stored object.") from exc
        except BotoCoreError as exc:
            raise StorageError("Failed to check stored object.") from exc
        return True

    async def get_bytes(self, location: str) -> bytes:
        key = self._key_for(location)
        try:
            async with self._client() as s3:
                response = await s3.get_object(Bucket=self._bucket, Key=key)
                async with response["Body"] as body:
                    return await body.read()
        except ClientError as exc:
            if _is_not_found(exc):
                raise StorageError(f"No stored object at {location!r}.") from exc
            raise StorageError("Failed to read stored object.") from exc
        except BotoCoreError as exc:
            raise StorageError("Failed to read stored object.") from exc

    async def delete(self, document_id: uuid.UUID | str) -> None:
        key = self._key_for(self.location_for(document_id))
        try:
            async with self._client() as s3:
                await s3.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return
            raise StorageError(f"Failed to delete document {document_id}.") from exc
        except BotoCoreError as exc:
            raise StorageError(f"Failed to delete document {document_id}.") from exc

    async def health_check(self) -> None:
        """Cheap reachability probe for ``/health/ready``. Raises on failure."""
        try:
            async with self._client() as s3:
                await s3.head_bucket(Bucket=self._bucket)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("Object storage is not reachable.") from exc
