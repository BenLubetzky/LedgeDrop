"""Storage interface shared by every original-PDF backend.

A backend maps a storage-root-relative location
(``<document_id>/original.pdf``, the value persisted in
``documents.file_location``) to stored bytes. The location shape is identical
across backends, so a row written under local development still resolves after a
cutover to object storage.

Design rules every implementation must uphold:

* A write is atomic at the unit the backend exposes - a crashed or failed
  request never leaves a partially written / partially visible original.
* A location that is absolute, contains ``..``, or otherwise escapes the
  storage root is rejected with :class:`StorageError` before any I/O.
* ``delete`` is idempotent and safe to call during cleanup even if nothing was
  written.
"""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass
from pathlib import Path


class StorageError(RuntimeError):
    """Raised when a storage operation fails or is asked to act outside its root."""


@dataclass(frozen=True)
class StoredFile:
    """Result of a successful write.

    ``path`` is the absolute on-disk path for a filesystem backend and ``None``
    for an object-store backend, which has no local path.
    """

    location: str  # storage-root-relative POSIX path, for documents.file_location
    path: Path | None
    size_bytes: int


class FileStorage(abc.ABC):
    """Abstract original-PDF store. See the module docstring for the contract."""

    @abc.abstractmethod
    def location_for(self, document_id: uuid.UUID | str) -> str:
        """Return the canonical storage-root-relative location for a document."""

    @abc.abstractmethod
    async def save_bytes(self, document_id: uuid.UUID | str, content: bytes) -> StoredFile:
        """Atomically store ``content`` as the document's original PDF."""

    @abc.abstractmethod
    async def exists(self, location: str) -> bool:
        """Return whether a stored object exists at ``location``."""

    @abc.abstractmethod
    async def get_bytes(self, location: str) -> bytes:
        """Return the stored bytes at ``location``.

        Raises :class:`StorageError` if ``location`` escapes the root or nothing
        is stored there. Callers stream the result to a client; it is never a
        filesystem path.
        """

    @abc.abstractmethod
    async def delete(self, document_id: uuid.UUID | str) -> None:
        """Remove everything stored for a document. No-op if nothing exists."""
