"""Local filesystem storage for original uploaded PDFs.

Layout under the configured storage root::

    <root>/<document_id>/original.pdf

The value persisted in ``documents.file_location`` is the path *relative to the
storage root* (``<document_id>/original.pdf``), so the physical root can move
between environments without rewriting rows.

Design rules enforced here:

* Writes are atomic - bytes go to a temporary file in the destination directory
  and are ``os.replace``-d into place only after a successful flush + fsync, so a
  crashed or failed request never leaves a half-written ``original.pdf``.
* :meth:`LocalFileStorage.resolve` refuses any location that is absolute or that
  escapes the storage root, so a caller can never coax the service into reading
  or writing an arbitrary filesystem path.
* :meth:`LocalFileStorage.delete` removes a document's whole directory and is
  safe to call during cleanup even if nothing was written.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import anyio

ORIGINAL_FILENAME = "original.pdf"


class StorageError(RuntimeError):
    """Raised when a storage operation fails or is asked to act outside its root."""


@dataclass(frozen=True)
class StoredFile:
    """Result of a successful write."""

    location: str  # storage-root-relative POSIX path, for documents.file_location
    path: Path  # absolute path on this machine
    size_bytes: int


class LocalFileStorage:
    def __init__(self, base_dir: str | os.PathLike[str]) -> None:
        self._base_dir = Path(base_dir).resolve()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    # -- path handling ---------------------------------------------------- --

    def location_for(self, document_id: uuid.UUID | str) -> str:
        """Return the canonical storage-root-relative location for a document."""
        return str(PurePosixPath(str(document_id)) / ORIGINAL_FILENAME)

    def resolve(self, location: str) -> Path:
        """Resolve a stored location to an absolute path inside the storage root.

        Raises :class:`StorageError` if ``location`` is absolute, contains a
        drive, or would escape the root via ``..`` segments.
        """
        candidate = PurePosixPath(location)
        if candidate.is_absolute() or Path(location).is_absolute() or Path(location).drive:
            raise StorageError("Storage location must be relative.")

        resolved = (self._base_dir / Path(*candidate.parts)).resolve()
        if resolved != self._base_dir and self._base_dir not in resolved.parents:
            raise StorageError("Storage location escapes the storage root.")
        return resolved

    # -- operations ----------------------------------------------------- ----

    async def save_bytes(self, document_id: uuid.UUID | str, content: bytes) -> StoredFile:
        """Atomically write ``content`` as the document's original PDF."""
        location = self.location_for(document_id)
        dest = self.resolve(location)
        try:
            await anyio.to_thread.run_sync(_atomic_write, dest, content)
        except OSError as exc:  # pragma: no cover - filesystem failure
            # Best-effort cleanup of a possibly-created directory.
            await self.delete(document_id)
            raise StorageError(f"Failed to store document {document_id}.") from exc
        return StoredFile(location=location, path=dest, size_bytes=len(content))

    async def exists(self, location: str) -> bool:
        path = self.resolve(location)
        return await anyio.to_thread.run_sync(path.is_file)

    async def path_for(self, location: str) -> Path:
        """Resolve ``location`` to an existing file path inside the storage root.

        Raises :class:`StorageError` if the location escapes the root (see
        :meth:`resolve`) or if no file exists there. Callers use the returned
        path only to stream the file; it is never surfaced to API clients.
        """
        path = self.resolve(location)
        if not await anyio.to_thread.run_sync(path.is_file):
            raise StorageError(f"No stored file at {location!r}.")
        return path

    async def delete(self, document_id: uuid.UUID | str) -> None:
        """Remove a document's directory. No-op if it does not exist."""
        target = (self._base_dir / str(document_id)).resolve()
        if target == self._base_dir or self._base_dir not in target.parents:
            raise StorageError("Refusing to delete outside the storage root.")
        await anyio.to_thread.run_sync(lambda: shutil.rmtree(target, ignore_errors=True))


def _atomic_write(dest: Path, content: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-", suffix=".pdf")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, dest)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
