"""Tests for ``POST /documents``."""

from __future__ import annotations

import hashlib
import uuid
from io import BytesIO

import pytest
from httpx import AsyncClient
from pypdf import PdfWriter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.document import Document, DocumentStatus
from app.services.storage import LocalFileStorage


def make_pdf(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def upload(data: bytes, filename: str = "invoice.pdf", content_type: str = "application/pdf"):
    return {"file": (filename, data, content_type)}


async def _count_documents(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Document))


async def test_successful_upload(
    client: AsyncClient, db_session: AsyncSession, storage: LocalFileStorage
) -> None:
    data = make_pdf(pages=2)

    response = await client.post("/documents", files=upload(data, "invoice-2048.pdf"))

    assert response.status_code == 201
    body = response.json()
    assert uuid.UUID(body["document_id"])
    assert body["original_filename"] == "invoice-2048.pdf"
    assert body["file_size_bytes"] == len(data)
    assert body["page_count"] == 2
    assert body["status"] == "UPLOADED"
    assert body["uploaded_at"].endswith("Z")
    # Internal fields are never exposed.
    assert "file_location" not in body
    assert "file_hash" not in body

    row = (await db_session.execute(select(Document))).scalar_one()
    assert str(row.document_id) == body["document_id"]
    assert row.status is DocumentStatus.UPLOADED
    assert row.file_location == f"{row.document_id}/original.pdf"
    assert row.file_hash == hashlib.sha256(data).hexdigest()
    assert row.file_size_bytes == len(data)

    stored_path = storage.resolve(row.file_location)
    assert stored_path.is_file()
    assert stored_path.read_bytes() == data


async def test_missing_file_is_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    response = await client.post("/documents")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert await _count_documents(db_session) == 0


async def test_blank_filename_is_rejected(client: AsyncClient) -> None:
    # A multipart file part that carries an explicitly empty filename (some
    # clients send this); httpx omits the attribute entirely for filename="",
    # so the body is built by hand here.
    body = (
        b"--B\r\n"
        b'Content-Disposition: form-data; name="file"; filename=""\r\n'
        b"Content-Type: application/pdf\r\n\r\n" + make_pdf() + b"\r\n--B--\r\n"
    )
    response = await client.post(
        "/documents",
        content=body,
        headers={"Content-Type": "multipart/form-data; boundary=B"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "FILE_REQUIRED"


async def test_overlong_filename_is_rejected(
    client: AsyncClient, db_session: AsyncSession, storage: LocalFileStorage
) -> None:
    filename = f"{'a' * 509}.pdf"
    response = await client.post("/documents", files=upload(make_pdf(), filename))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "FILENAME_TOO_LONG"
    assert await _count_documents(db_session) == 0
    assert not storage.base_dir.exists() or not any(storage.base_dir.iterdir())


async def test_non_pdf_extension_is_rejected(
    client: AsyncClient, db_session: AsyncSession, storage: LocalFileStorage
) -> None:
    response = await client.post(
        "/documents", files=upload(b"just some text", "notes.txt", "text/plain")
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "NOT_A_PDF"
    assert await _count_documents(db_session) == 0
    assert not storage.base_dir.exists() or not any(storage.base_dir.iterdir())


async def test_pdf_extension_but_not_pdf_content_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.post(
        "/documents", files=upload(b"this is definitely not a pdf", "invoice.pdf")
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "NOT_A_PDF"
    assert await _count_documents(db_session) == 0


async def test_corrupt_pdf_is_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    corrupt = b"%PDF-1.4\n" + b"garbage bytes with no valid structure \x00\x01\x02"
    response = await client.post("/documents", files=upload(corrupt))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PDF_UNREADABLE"
    assert await _count_documents(db_session) == 0


async def test_file_over_size_limit_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    oversized = b"%PDF-1.4\n" + b"\x00" * (settings.max_file_size_bytes + 1)
    response = await client.post("/documents", files=upload(oversized))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert await _count_documents(db_session) == 0


async def test_pdf_over_page_limit_is_rejected(
    client: AsyncClient, db_session: AsyncSession, storage: LocalFileStorage
) -> None:
    data = make_pdf(pages=settings.max_pdf_pages + 1)
    response = await client.post("/documents", files=upload(data))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TOO_MANY_PAGES"
    assert await _count_documents(db_session) == 0
    assert not storage.base_dir.exists() or not any(storage.base_dir.iterdir())


async def test_database_failure_removes_stored_file(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: LocalFileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(self) -> None:  # noqa: ANN001
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(AsyncSession, "flush", boom)

    response = await client.post("/documents", files=upload(make_pdf()))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert await _count_documents(db_session) == 0
    # No orphaned file or directory left behind.
    assert not storage.base_dir.exists() or not any(storage.base_dir.iterdir())


async def test_database_commit_failure_removes_stored_file(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: LocalFileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(self) -> None:  # noqa: ANN001
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(AsyncSession, "commit", boom)

    response = await client.post("/documents", files=upload(make_pdf()))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert await _count_documents(db_session) == 0
    assert not storage.base_dir.exists() or not any(storage.base_dir.iterdir())


async def test_storage_failure_creates_no_record(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: LocalFileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(self, document_id, content):  # noqa: ANN001
        raise RuntimeError("simulated storage failure")

    monkeypatch.setattr(LocalFileStorage, "save_bytes", boom)

    response = await client.post("/documents", files=upload(make_pdf()))

    assert response.status_code == 500
    assert await _count_documents(db_session) == 0
