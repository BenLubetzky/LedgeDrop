"""Tests for ``DELETE /documents/{document_id}``."""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from app.services.storage import LocalFileStorage
from tests._helpers import make_pdf


async def test_delete_removes_metadata_and_stored_pdf(
    client: AsyncClient, storage: LocalFileStorage
) -> None:
    created = await client.post(
        "/documents",
        files={"file": ("invoice.pdf", make_pdf(), "application/pdf")},
    )
    assert created.status_code == 201
    document_id = created.json()["document_id"]
    location = storage.location_for(document_id)
    assert await storage.exists(location)

    response = await client.delete(f"/documents/{document_id}")

    assert response.status_code == 204
    assert response.content == b""
    assert (await client.get(f"/documents/{document_id}")).status_code == 404
    assert not await storage.exists(location)


async def test_delete_unknown_document_returns_404(client: AsyncClient) -> None:
    response = await client.delete(f"/documents/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"
