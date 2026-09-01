from __future__ import annotations

import uuid

import pytest

from app.services.storage import LocalFileStorage, StorageError


@pytest.fixture
def storage(tmp_path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "uploads")


async def test_save_bytes_writes_file_and_returns_location(storage: LocalFileStorage) -> None:
    document_id = uuid.uuid4()
    stored = await storage.save_bytes(document_id, b"%PDF-1.7 fake")

    assert stored.location == f"{document_id}/original.pdf"
    assert stored.size_bytes == len(b"%PDF-1.7 fake")
    assert stored.path.is_file()
    assert stored.path.read_bytes() == b"%PDF-1.7 fake"
    # No leftover temporary files in the document directory.
    assert [p.name for p in stored.path.parent.iterdir()] == ["original.pdf"]


async def test_exists_reflects_state(storage: LocalFileStorage) -> None:
    document_id = uuid.uuid4()
    location = storage.location_for(document_id)
    assert await storage.exists(location) is False
    await storage.save_bytes(document_id, b"data")
    assert await storage.exists(location) is True


async def test_delete_removes_document_directory(storage: LocalFileStorage) -> None:
    document_id = uuid.uuid4()
    stored = await storage.save_bytes(document_id, b"data")
    await storage.delete(document_id)
    assert not stored.path.exists()
    assert not stored.path.parent.exists()
    # Deleting again is a no-op.
    await storage.delete(document_id)


@pytest.mark.parametrize(
    "bad_location",
    [
        "../escape.pdf",
        "../../etc/passwd",
        "sub/../../outside.pdf",
        "/absolute/path.pdf",
    ],
)
async def test_resolve_rejects_path_traversal(storage: LocalFileStorage, bad_location: str) -> None:
    with pytest.raises(StorageError):
        storage.resolve(bad_location)


async def test_resolve_accepts_canonical_location(storage: LocalFileStorage) -> None:
    document_id = uuid.uuid4()
    resolved = storage.resolve(storage.location_for(document_id))
    assert resolved.parent.parent == storage.base_dir
    assert resolved.name == "original.pdf"
