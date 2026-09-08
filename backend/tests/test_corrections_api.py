"""Focused tests for reviewer correction, revalidation, and approval."""

from __future__ import annotations

from httpx import AsyncClient

from tests._helpers import make_pdf


async def _reviewable_document(client: AsyncClient) -> tuple[str, str]:
    uploaded = await client.post(
        "/documents", files={"file": ("invoice.pdf", make_pdf(1), "application/pdf")}
    )
    document_id = uploaded.json()["document_id"]
    pipeline = await client.post(
        f"/documents/{document_id}/pipeline",
        json={"manual_review_requested": True},
    )
    assert pipeline.status_code == 201, pipeline.text
    assert pipeline.json()["decision"]["outcome"] == "NEEDS_REVIEW"
    return document_id, pipeline.json()["decision"]["decision_id"]


def _correction(decision_id: str) -> dict[str, object]:
    return {
        "source_decision_id": decision_id,
        "reviewer_name": "Dana Ops",
        "note": "Checked against the PDF",
        "invoice_number": "INV-100",
        "invoice_date": "2026-09-01",
        "due_date": "2026-09-30",
        "vendor_name": "Example Supplier Ltd",
        "vendor_tax_id": "PT123456789",
        "customer_name": "Example Customer",
        "currency": "EUR",
        "subtotal": "100.00",
        "tax_amount": "20.00",
        "total_amount": "120.00",
        "line_items": [
            {
                "description": "Service",
                "quantity": "1",
                "unit_price": "100.00",
                "line_total": "100.00",
            }
        ],
    }


async def test_valid_correction_is_reprocessed_and_approved(client: AsyncClient) -> None:
    document_id, decision_id = await _reviewable_document(client)
    response = await client.post(
        f"/documents/{document_id}/corrections", json=_correction(decision_id)
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["approved"] is True
    assert body["extraction"]["provider_name"] == "human-correction"
    assert body["normalization"]["data"]["currency"] == "EUR"
    assert body["decision"]["outcome"] == "ACCEPTED"
    document = await client.get(f"/documents/{document_id}")
    assert document.json()["status"] == "APPROVED"


async def test_invalid_correction_stays_in_review(client: AsyncClient) -> None:
    document_id, decision_id = await _reviewable_document(client)
    correction = _correction(decision_id)
    correction["invoice_number"] = None
    response = await client.post(
        f"/documents/{document_id}/corrections", json=correction
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["approved"] is False
    assert body["decision"]["outcome"] == "NEEDS_REVIEW"
    document = await client.get(f"/documents/{document_id}")
    assert document.json()["status"] == "NEEDS_REVIEW"


async def test_completed_invoice_can_be_edited_and_revalidated(client: AsyncClient) -> None:
    uploaded = await client.post(
        "/documents", files={"file": ("invoice.pdf", make_pdf(1), "application/pdf")}
    )
    document_id = uploaded.json()["document_id"]
    pipeline = await client.post(f"/documents/{document_id}/pipeline")
    extraction_id = pipeline.json()["extraction"]["extraction_id"]
    correction = _correction(pipeline.json()["decision"]["decision_id"])
    correction.pop("source_decision_id")
    correction["source_extraction_id"] = extraction_id

    response = await client.post(
        f"/documents/{document_id}/corrections", json=correction
    )
    assert response.status_code == 201, response.text
    assert response.json()["approved"] is True
    assert response.json()["extraction"]["extraction_id"] != extraction_id
    document = await client.get(f"/documents/{document_id}")
    assert document.json()["status"] == "COMPLETED"
