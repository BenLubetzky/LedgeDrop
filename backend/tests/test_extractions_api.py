"""Tests for the extraction API endpoints (Stage 3, step 7).

These drive the real HTTP layer through the ``client`` fixture. The extractor is
the deterministic offline fake; tests that need a failure override
``get_extractor`` on the ``app`` fixture.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.api.deps import get_extractor
from app.services.processing.extraction.fake import (
    FakeBehavior,
    FakeExtractionProvider,
    deterministic_invoice_payload,
)
from app.services.processing.extraction.preprocessing import PreparedDocument, TextLayer

from tests._helpers import make_pdf


async def _upload(client: AsyncClient, name: str = "invoice.pdf") -> str:
    resp = await client.post(
        "/documents", files={"file": (name, make_pdf(1), "application/pdf")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["document_id"]


def _install_provider(app, provider) -> None:
    app.dependency_overrides[get_extractor] = lambda: provider


def _install_failing_extractor(
    app, *, behavior: FakeBehavior = FakeBehavior.TIMEOUT
) -> None:
    _install_provider(app, FakeExtractionProvider(behavior))


# --- start -------------------------------------------------------------


async def test_start_extraction_returns_a_completed_result(client: AsyncClient) -> None:
    document_id = await _upload(client)

    resp = await client.post(f"/documents/{document_id}/extractions")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["attempt_number"] == 1
    assert body["document_id"] == document_id
    assert body["provider_name"] == "fake-deterministic"
    assert body["failure_code"] is None
    assert body["data"]["invoice_number"]["value"].startswith("INV-")
    assert body["data"]["total_amount"]["value"] == "119.00"
    assert len(body["data"]["line_items"]) == 1
    assert "raw_response" not in body

    doc = (await client.get(f"/documents/{document_id}")).json()
    assert doc["status"] == "COMPLETED"


async def test_start_preprocesses_the_stored_pdf_before_fake_extraction(
    client: AsyncClient, app
) -> None:
    seen: dict[str, object] = {}

    class ObservingProvider:
        name = "observing"

        async def extract(self, prepared: PreparedDocument):
            seen["document_id"] = prepared.document_id
            seen["text_layer"] = prepared.text_layer
            seen["ocr_pages"] = prepared.ocr_page_numbers
            return deterministic_invoice_payload(prepared.document_id)

    _install_provider(app, ObservingProvider())
    document_id = await _upload(client)

    response = await client.post(f"/documents/{document_id}/extractions")

    assert response.status_code == 201
    assert seen == {
        "document_id": uuid.UUID(document_id),
        "text_layer": TextLayer.ABSENT,
        "ocr_pages": (1,),
    }


async def test_preprocessing_failure_becomes_a_safe_failed_attempt(
    client: AsyncClient, storage
) -> None:
    document_id = await _upload(client)
    await storage.delete(document_id)

    response = await client.post(f"/documents/{document_id}/extractions")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["failure_code"] == "PDF_UNAVAILABLE"
    assert body["failure_message"] == "The stored PDF for this document is unavailable."
    assert "file_location" not in response.text


async def test_start_extraction_is_deterministic_for_a_document(client: AsyncClient) -> None:
    a = await _upload(client)
    b = await _upload(client)
    first = (await client.post(f"/documents/{a}/extractions")).json()
    other = (await client.post(f"/documents/{b}/extractions")).json()

    assert first["data"]["invoice_number"]["value"] != other["data"]["invoice_number"]["value"]


@pytest.mark.parametrize("payload", [None, {}])
async def test_start_extraction_accepts_an_empty_body(client: AsyncClient, payload) -> None:
    document_id = await _upload(client)
    resp = await client.post(f"/documents/{document_id}/extractions", json=payload)
    assert resp.status_code == 201, resp.text


async def test_start_extraction_rejects_unknown_body_keys(client: AsyncClient) -> None:
    document_id = await _upload(client)
    resp = await client.post(
        f"/documents/{document_id}/extractions", json={"provider": "anthropic"}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_start_extraction_unknown_document_is_404(client: AsyncClient) -> None:
    resp = await client.post(f"/documents/{uuid.uuid4()}/extractions")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


async def test_start_extraction_conflict_when_already_extracted(client: AsyncClient) -> None:
    document_id = await _upload(client)
    assert (await client.post(f"/documents/{document_id}/extractions")).status_code == 201

    resp = await client.post(f"/documents/{document_id}/extractions")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DOCUMENT_ALREADY_EXTRACTED"


async def test_start_extraction_that_fails_still_returns_201_with_a_failed_body(
    client: AsyncClient, app
) -> None:
    _install_failing_extractor(app, behavior=FakeBehavior.RATE_LIMITED)
    document_id = await _upload(client)

    resp = await client.post(f"/documents/{document_id}/extractions")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["failure_code"] == "PROVIDER_RATE_LIMITED"
    assert body["data"]["invoice_number"]["value"] is None

    doc = (await client.get(f"/documents/{document_id}")).json()
    assert doc["status"] == "FAILED"


# --- retry -----------------------------------------------------------


async def test_retry_after_a_failed_extraction_completes_as_attempt_two(
    client: AsyncClient, app
) -> None:
    _install_failing_extractor(app)
    document_id = await _upload(client)
    failed = (await client.post(f"/documents/{document_id}/extractions")).json()
    assert failed["status"] == "FAILED"

    app.dependency_overrides[get_extractor] = lambda: FakeExtractionProvider()

    resp = await client.post(f"/documents/{document_id}/extractions/retry")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["attempt_number"] == 2

    doc = (await client.get(f"/documents/{document_id}")).json()
    assert doc["status"] == "COMPLETED"


async def test_retry_conflict_when_extraction_did_not_fail(client: AsyncClient) -> None:
    document_id = await _upload(client)

    resp = await client.post(f"/documents/{document_id}/extractions/retry")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EXTRACTION_NOT_FAILED"


async def test_retry_unknown_document_is_404(client: AsyncClient) -> None:
    resp = await client.post(f"/documents/{uuid.uuid4()}/extractions/retry")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


# --- retrieval -----------------------------------------------------


async def test_latest_extraction_returns_the_newest_attempt(
    client: AsyncClient, app
) -> None:
    _install_failing_extractor(app)
    document_id = await _upload(client)
    await client.post(f"/documents/{document_id}/extractions")
    app.dependency_overrides[get_extractor] = lambda: FakeExtractionProvider()
    await client.post(f"/documents/{document_id}/extractions/retry")

    resp = await client.get(f"/documents/{document_id}/extractions/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["attempt_number"] == 2
    assert body["status"] == "COMPLETED"


async def test_latest_extraction_404_when_none_yet(client: AsyncClient) -> None:
    document_id = await _upload(client)
    resp = await client.get(f"/documents/{document_id}/extractions/latest")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "EXTRACTION_NOT_FOUND"


async def test_list_extractions_is_newest_first(client: AsyncClient, app) -> None:
    _install_failing_extractor(app)
    document_id = await _upload(client)
    await client.post(f"/documents/{document_id}/extractions")
    app.dependency_overrides[get_extractor] = lambda: FakeExtractionProvider()
    await client.post(f"/documents/{document_id}/extractions/retry")

    resp = await client.get(f"/documents/{document_id}/extractions")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["attempt_number"] for r in rows] == [2, 1]
    assert [r["status"] for r in rows] == ["COMPLETED", "FAILED"]


async def test_get_specific_extraction_scoped_to_its_document(client: AsyncClient) -> None:
    doc_a = await _upload(client)
    doc_b = await _upload(client)
    created = (await client.post(f"/documents/{doc_a}/extractions")).json()
    extraction_id = created["extraction_id"]

    ok = await client.get(f"/documents/{doc_a}/extractions/{extraction_id}")
    assert ok.status_code == 200
    assert ok.json()["extraction_id"] == extraction_id

    wrong_doc = await client.get(f"/documents/{doc_b}/extractions/{extraction_id}")
    assert wrong_doc.status_code == 404
    assert wrong_doc.json()["error"]["code"] == "EXTRACTION_NOT_FOUND"

    unknown = await client.get(f"/documents/{doc_a}/extractions/{uuid.uuid4()}")
    assert unknown.status_code == 404


async def test_retrieval_endpoints_404_for_unknown_document(client: AsyncClient) -> None:
    missing = uuid.uuid4()
    assert (await client.get(f"/documents/{missing}/extractions")).status_code == 404
    assert (await client.get(f"/documents/{missing}/extractions/latest")).status_code == 404
    assert (
        await client.get(f"/documents/{missing}/extractions/{uuid.uuid4()}")
    ).status_code == 404


# --- Stage 2 stays intact -------------------------------------------


async def test_stage_2_document_endpoints_still_work(client: AsyncClient) -> None:
    document_id = await _upload(client)

    listing = await client.get("/documents")
    assert listing.status_code == 200
    assert any(d["document_id"] == document_id for d in listing.json())

    file_resp = await client.get(f"/documents/{document_id}/file")
    assert file_resp.status_code == 200
    assert file_resp.headers["content-type"] == "application/pdf"
