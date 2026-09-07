"""Tests for ``DELETE /documents/{document_id}``."""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from app.api.deps import get_extractor
from app.services.processing.extraction.fake import deterministic_invoice_payload
from app.services.storage import LocalFileStorage
from tests._helpers import make_pdf


class _HighValueExtractor:
    name = "high-value-delete-regression"
    model = "test"

    async def extract(self, prepared):
        payload = deterministic_invoice_payload(prepared.document_id)
        payload["subtotal"]["value"] = "10000.00"
        payload["tax_amount"]["value"] = "1900.00"
        payload["total_amount"]["value"] = "11900.00"
        payload["line_items"][0]["quantity"]["value"] = "1"
        payload["line_items"][0]["unit_price"]["value"] = "10000.00"
        payload["line_items"][0]["line_total"]["value"] = "10000.00"
        return payload


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


async def test_delete_approved_document_removes_full_processing_chain(
    app, client: AsyncClient, storage: LocalFileStorage
) -> None:
    app.dependency_overrides[get_extractor] = lambda: _HighValueExtractor()
    created = await client.post(
        "/documents",
        files={"file": ("invoice.pdf", make_pdf(), "application/pdf")},
    )
    assert created.status_code == 201
    document_id = created.json()["document_id"]
    location = storage.location_for(document_id)

    pipeline = await client.post(f"/documents/{document_id}/pipeline")
    assert pipeline.status_code == 201, pipeline.text
    body = pipeline.json()
    assert body["decision"]["outcome"] == "NEEDS_REVIEW"

    review_url = (
        f"/documents/{document_id}/extractions/{body['extraction']['extraction_id']}"
        f"/normalizations/{body['normalization']['normalization_id']}"
        f"/validations/{body['validation']['validation_id']}"
        f"/decisions/{body['decision']['decision_id']}/review"
    )
    reviewed = await client.post(
        review_url,
        json={"action": "APPROVE", "reviewer_name": "Delete regression test"},
    )
    assert reviewed.status_code == 201, reviewed.text

    response = await client.delete(f"/documents/{document_id}")

    assert response.status_code == 204, response.text
    assert (await client.get(f"/documents/{document_id}")).status_code == 404
    assert not await storage.exists(location)
