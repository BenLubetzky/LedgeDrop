"""Stage 8 correction API tests - the composed upload -> pipeline -> correct
flow over real HTTP, against the deterministic fake extraction provider."""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from app.services.processing.correction import service as correction_service_module

from tests._helpers import make_pdf


async def _pipeline_document(
    client: AsyncClient, *, manual_review: bool
) -> tuple[str, dict]:
    uploaded = await client.post(
        "/documents", files={"file": ("invoice.pdf", make_pdf(1), "application/pdf")}
    )
    document_id = uploaded.json()["document_id"]
    pipeline = await client.post(
        f"/documents/{document_id}/pipeline",
        json={"manual_review_requested": manual_review},
    )
    assert pipeline.status_code == 201, pipeline.text
    return document_id, pipeline.json()


async def _current_normalized(client: AsyncClient, document_id: str) -> dict:
    extraction = (
        await client.get(f"/documents/{document_id}/extractions/latest")
    ).json()
    normalization = (
        await client.get(
            f"/documents/{document_id}/extractions/{extraction['extraction_id']}"
            "/normalizations/latest"
        )
    ).json()
    return normalization["data"]


def _full_correction(decision_id: str, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "source_decision_id": decision_id,
        "reviewer_name": "Dana Ops",
        "note": "Checked against the PDF",
        "invoice_number": "INV-STAGE8",
        "invoice_date": "2026-09-01",
        "due_date": "2026-09-30",
        "vendor_name": "Corrected Supplier Ltd",
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
    body.update(overrides)
    return body


async def test_clean_correction_from_needs_review_auto_approves(
    client: AsyncClient,
) -> None:
    document_id, pipeline = await _pipeline_document(client, manual_review=True)
    assert pipeline["decision"]["outcome"] == "NEEDS_REVIEW"
    decision_id = pipeline["decision"]["decision_id"]

    response = await client.post(
        f"/documents/{document_id}/corrections", json=_full_correction(decision_id)
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["origin_document_status"] == "NEEDS_REVIEW"
    assert body["resulting_outcome"] == "ACCEPTED"
    assert body["resulting_document_status"] == "APPROVED"
    assert body["attempt_number"] == 1
    assert len(body["entries"]) > 0
    assert body["pipeline"]["decision"]["outcome"] == "ACCEPTED"

    document = await client.get(f"/documents/{document_id}")
    assert document.json()["status"] == "APPROVED"


async def test_correction_that_still_gates_returns_to_review(
    client: AsyncClient,
) -> None:
    document_id, pipeline = await _pipeline_document(client, manual_review=True)
    decision_id = pipeline["decision"]["decision_id"]

    response = await client.post(
        f"/documents/{document_id}/corrections",
        json=_full_correction(decision_id, invoice_number=None),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["resulting_outcome"] == "NEEDS_REVIEW"
    assert body["resulting_document_status"] == "NEEDS_REVIEW"

    document = await client.get(f"/documents/{document_id}")
    assert document.json()["status"] == "NEEDS_REVIEW"

    queue = await client.get("/reviews/queue")
    assert any(entry["document_id"] == document_id for entry in queue.json())


async def test_clean_correction_from_completed_stays_completed(
    client: AsyncClient,
) -> None:
    document_id, pipeline = await _pipeline_document(client, manual_review=False)
    assert pipeline["decision"]["outcome"] == "ACCEPTED"
    decision_id = pipeline["decision"]["decision_id"]

    response = await client.post(
        f"/documents/{document_id}/corrections",
        json=_full_correction(decision_id, vendor_name="Another Corrected Vendor"),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["origin_document_status"] == "COMPLETED"
    assert body["resulting_outcome"] == "ACCEPTED"
    assert body["resulting_document_status"] == "COMPLETED"

    document = await client.get(f"/documents/{document_id}")
    assert document.json()["status"] == "COMPLETED"


async def test_repeated_corrections_chain_and_keep_history(client: AsyncClient) -> None:
    document_id, pipeline = await _pipeline_document(client, manual_review=True)
    decision_id = pipeline["decision"]["decision_id"]

    first = await client.post(
        f"/documents/{document_id}/corrections", json=_full_correction(decision_id)
    )
    assert first.status_code == 201, first.text
    new_decision_id = first.json()["pipeline"]["decision"]["decision_id"]

    second = await client.post(
        f"/documents/{document_id}/corrections",
        json=_full_correction(new_decision_id, customer_name="Second Pass Customer"),
    )
    assert second.status_code == 201, second.text
    assert second.json()["attempt_number"] == 2

    listing = await client.get(f"/documents/{document_id}/corrections")
    assert [row["attempt_number"] for row in listing.json()] == [2, 1]


async def test_stale_source_decision_is_rejected(client: AsyncClient) -> None:
    document_id, _ = await _pipeline_document(client, manual_review=True)
    response = await client.post(
        f"/documents/{document_id}/corrections",
        json=_full_correction(str(uuid.uuid4())),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_CORRECTION_SOURCE"


async def test_uploaded_document_is_not_correctable(client: AsyncClient) -> None:
    uploaded = await client.post(
        "/documents", files={"file": ("invoice.pdf", make_pdf(1), "application/pdf")}
    )
    document_id = uploaded.json()["document_id"]
    response = await client.post(
        f"/documents/{document_id}/corrections",
        json=_full_correction(str(uuid.uuid4())),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_CORRECTABLE"


async def test_no_op_correction_is_422(client: AsyncClient) -> None:
    document_id, pipeline = await _pipeline_document(client, manual_review=True)
    decision_id = pipeline["decision"]["decision_id"]
    normalized = await _current_normalized(client, document_id)

    echo = {
        "source_decision_id": decision_id,
        "reviewer_name": "Dana Ops",
        **{
            key: normalized[key]
            for key in (
                "invoice_number",
                "invoice_date",
                "due_date",
                "vendor_name",
                "vendor_tax_id",
                "customer_name",
                "currency",
                "subtotal",
                "tax_amount",
                "total_amount",
            )
        },
        "line_items": [
            {k: item[k] for k in ("description", "quantity", "unit_price", "line_total")}
            for item in normalized["line_items"]
        ],
    }
    response = await client.post(f"/documents/{document_id}/corrections", json=echo)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CORRECTION_EMPTY"


async def test_unknown_document_is_404(client: AsyncClient) -> None:
    response = await client.post(
        f"/documents/{uuid.uuid4()}/corrections",
        json=_full_correction(str(uuid.uuid4())),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


async def test_get_and_latest_and_missing(client: AsyncClient) -> None:
    document_id, pipeline = await _pipeline_document(client, manual_review=True)
    decision_id = pipeline["decision"]["decision_id"]
    created = await client.post(
        f"/documents/{document_id}/corrections", json=_full_correction(decision_id)
    )
    correction_id = created.json()["correction_id"]

    one = await client.get(f"/documents/{document_id}/corrections/{correction_id}")
    assert one.status_code == 200
    assert one.json()["correction_id"] == correction_id

    latest = await client.get(f"/documents/{document_id}/corrections/latest")
    assert latest.json()["correction_id"] == correction_id

    missing = await client.get(
        f"/documents/{document_id}/corrections/{uuid.uuid4()}"
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "CORRECTION_NOT_FOUND"


async def test_retry_after_failure_past_projection_replays_original_source(
    client: AsyncClient, monkeypatch,
) -> None:
    """A persisted projection must not turn retry into a false no-op."""
    document_id, pipeline = await _pipeline_document(client, manual_review=True)
    decision_id = pipeline["decision"]["decision_id"]
    original_start = correction_service_module.ValidationService.start

    async def fail_validation_once(self, normalization_id):
        raise RuntimeError("injected failure after projection")

    monkeypatch.setattr(
        correction_service_module.ValidationService, "start", fail_validation_once
    )
    failed = await client.post(
        f"/documents/{document_id}/corrections",
        json=_full_correction(decision_id),
    )
    assert failed.status_code == 201, failed.text
    assert failed.json()["status"] == "FAILED"
    assert failed.json()["pipeline"] is not None
    correction_id = failed.json()["correction_id"]

    monkeypatch.setattr(
        correction_service_module.ValidationService, "start", original_start
    )
    retried = await client.post(
        f"/documents/{document_id}/corrections/{correction_id}/retry"
    )
    assert retried.status_code == 201, retried.text
    body = retried.json()
    assert body["attempt_number"] == 2
    assert body["status"] == "COMPLETED"
    assert body["resulting_outcome"] == "ACCEPTED"
    assert body["resulting_document_status"] == "APPROVED"
