"""Tests for the document read endpoints: list, detail, and file streaming."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.services.storage import LocalFileStorage
from tests._helpers import make_pdf

_INTERNAL_FIELDS = {"file_location", "file_hash"}


async def _upload(client: AsyncClient, *, pages: int = 1, filename: str = "invoice.pdf") -> dict:
    response = await client.post(
        "/documents",
        files={"file": (filename, make_pdf(pages), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- GET /documents ---------------------------------------------------------


async def test_list_is_empty_when_no_uploads(client: AsyncClient) -> None:
    response = await client.get("/documents")
    assert response.status_code == 200
    assert response.json() == []


async def test_list_returns_all_newest_first(client: AsyncClient) -> None:
    uploaded = [await _upload(client, filename=f"doc-{i}.pdf") for i in range(3)]

    response = await client.get("/documents")
    assert response.status_code == 200
    body = response.json()

    assert {d["document_id"] for d in body} == {d["document_id"] for d in uploaded}
    timestamps = [d["uploaded_at"] for d in body]
    assert timestamps == sorted(timestamps, reverse=True)
    for item in body:
        assert _INTERNAL_FIELDS.isdisjoint(item)


# --- GET /documents/{id} --------------------------------------------------- -


async def test_get_one_document(client: AsyncClient) -> None:
    created = await _upload(client, pages=2, filename="invoice-77.pdf")

    response = await client.get(f"/documents/{created['document_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == created["document_id"]
    assert body["original_filename"] == "invoice-77.pdf"
    assert body["page_count"] == 2
    assert body["status"] == "UPLOADED"
    assert _INTERNAL_FIELDS.isdisjoint(body)


async def test_get_unknown_document_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/documents/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


async def test_get_document_with_malformed_id_returns_422(client: AsyncClient) -> None:
    response = await client.get("/documents/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --- GET /documents/{id}/file -------------------------------------------- ---


async def test_download_streams_the_stored_pdf(
    client: AsyncClient, storage: LocalFileStorage
) -> None:
    pdf = make_pdf(3)
    response = await client.post(
        "/documents", files={"file": ("statement.pdf", pdf, "application/pdf")}
    )
    document_id = response.json()["document_id"]

    downloaded = await client.get(f"/documents/{document_id}/file")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/pdf"
    assert downloaded.content == pdf

    disposition = downloaded.headers["content-disposition"]
    assert "inline" in disposition
    assert "statement.pdf" in disposition
    # The server's real filesystem path must never appear in the response.
    assert str(storage.base_dir) not in disposition
    assert str(storage.base_dir) not in downloaded.text


async def test_download_unknown_document_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/documents/{uuid.uuid4()}/file")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


async def test_download_returns_404_when_stored_file_is_missing(
    client: AsyncClient, db_session: AsyncSession, storage: LocalFileStorage
) -> None:
    created = await _upload(client)
    document_id = created["document_id"]

    # Remove the file from disk but leave the database row in place.
    await storage.delete(document_id)

    response = await client.get(f"/documents/{document_id}/file")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FILE_NOT_FOUND"

    # The row is still there and still fetchable as metadata.
    assert await db_session.get(Document, uuid.UUID(document_id)) is not None
    assert (await client.get(f"/documents/{document_id}")).status_code == 200
