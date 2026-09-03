"""Tests for the composed pipeline endpoint (Stage 4, step 13).

These drive the real HTTP layer through the ``client`` fixture. Extraction runs
on the deterministic offline fake (forced regardless of the machine's
``EXTRACTION_PROVIDER``); normalization has no provider. The per-stage
endpoints are exercised alongside the pipeline to prove they stay usable on
their own.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.api.deps import get_extractor
from app.services.processing.extraction.fake import FakeBehavior, FakeExtractionProvider

from tests._helpers import make_pdf

_ENGINE_PATH = "app.services.processing.normalization.service.normalize_extraction"


@pytest.fixture(autouse=True)
def _offline_extractor(app):
    app.dependency_overrides[get_extractor] = lambda: FakeExtractionProvider()
    yield


def _use_failing_extractor(app) -> None:
    app.dependency_overrides[get_extractor] = lambda: FakeExtractionProvider(
        FakeBehavior.TIMEOUT
    )


def _use_ok_extractor(app) -> None:
    app.dependency_overrides[get_extractor] = lambda: FakeExtractionProvider()


async def _upload(client: AsyncClient) -> str:
    resp = await client.post(
        "/documents", files={"file": ("invoice.pdf", make_pdf(1), "application/pdf")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["document_id"]


# --- happy path ------------------------------------------------------


async def test_run_pipeline_returns_both_stage_results(client: AsyncClient) -> None:
    document_id = await _upload(client)

    resp = await client.post(f"/documents/{document_id}/pipeline")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["extraction"]["status"] == "COMPLETED"
    assert body["extraction"]["document_id"] == document_id
    assert body["normalization"] is not None
    assert body["normalization"]["status"] == "COMPLETED"
    assert (
        body["normalization"]["extraction_id"] == body["extraction"]["extraction_id"]
    )
    assert body["normalization"]["attempt_number"] == 1
    assert body["normalization"]["data"]["errors"] == []
    # normalization view still hides internals
    assert "document_id" not in body["normalization"]
    assert "raw_response" not in resp.text

    doc = (await client.get(f"/documents/{document_id}")).json()
    assert doc["status"] == "COMPLETED"


async def test_pipeline_result_matches_the_per_stage_endpoints(
    client: AsyncClient,
) -> None:
    document_id = await _upload(client)
    body = (await client.post(f"/documents/{document_id}/pipeline")).json()
    extraction_id = body["extraction"]["extraction_id"]
    normalization_id = body["normalization"]["normalization_id"]

    latest_extraction = await client.get(
        f"/documents/{document_id}/extractions/latest"
    )
    assert latest_extraction.status_code == 200
    assert latest_extraction.json()["extraction_id"] == extraction_id

    latest_norm = await client.get(
        f"/documents/{document_id}/extractions/{extraction_id}/normalizations/latest"
    )
    assert latest_norm.status_code == 200
    assert latest_norm.json()["normalization_id"] == normalization_id


# --- extraction failure stops the chain ---------------------------


async def test_run_pipeline_stops_at_a_failed_extraction(
    client: AsyncClient, app
) -> None:
    _use_failing_extractor(app)
    document_id = await _upload(client)

    resp = await client.post(f"/documents/{document_id}/pipeline")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["extraction"]["status"] == "FAILED"
    assert body["normalization"] is None


async def test_run_pipeline_reports_a_normalization_technical_failure(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = await _upload(client)

    def boom(_contract):
        raise RuntimeError("engine crash key=sk-super-secret")

    monkeypatch.setattr(_ENGINE_PATH, boom)
    resp = await client.post(f"/documents/{document_id}/pipeline")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["extraction"]["status"] == "COMPLETED"
    assert body["normalization"]["status"] == "FAILED"
    assert body["normalization"]["failure_code"] == "NORMALIZATION_FAILED"
    assert "sk-super-secret" not in resp.text


# --- errors / conflicts ------------------------------------------


async def test_run_pipeline_unknown_document_is_404(client: AsyncClient) -> None:
    resp = await client.post(f"/documents/{uuid.uuid4()}/pipeline")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


async def test_run_pipeline_conflict_when_already_processed(
    client: AsyncClient,
) -> None:
    document_id = await _upload(client)
    assert (await client.post(f"/documents/{document_id}/pipeline")).status_code == 201

    resp = await client.post(f"/documents/{document_id}/pipeline")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DOCUMENT_ALREADY_EXTRACTED"


async def test_run_pipeline_rejects_unknown_body_keys(client: AsyncClient) -> None:
    document_id = await _upload(client)
    resp = await client.post(
        f"/documents/{document_id}/pipeline", json={"stage": "extraction"}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# --- retry ---------------------------------------------------------


async def test_retry_pipeline_after_a_failed_extraction_completes_both_stages(
    client: AsyncClient, app
) -> None:
    _use_failing_extractor(app)
    document_id = await _upload(client)
    failed = (await client.post(f"/documents/{document_id}/pipeline")).json()
    assert failed["extraction"]["status"] == "FAILED"
    assert failed["normalization"] is None

    _use_ok_extractor(app)
    resp = await client.post(f"/documents/{document_id}/pipeline/retry")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["extraction"]["status"] == "COMPLETED"
    assert body["extraction"]["attempt_number"] == 2
    assert body["normalization"]["status"] == "COMPLETED"
    assert (
        body["normalization"]["extraction_id"] == body["extraction"]["extraction_id"]
    )


async def test_retry_pipeline_conflict_when_extraction_did_not_fail(
    client: AsyncClient,
) -> None:
    document_id = await _upload(client)

    resp = await client.post(f"/documents/{document_id}/pipeline/retry")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EXTRACTION_NOT_FAILED"


# --- stages remain independently callable ------------------------


async def test_per_stage_endpoints_still_work_standalone(client: AsyncClient) -> None:
    # A document driven one stage at a time, no pipeline call.
    document_id = await _upload(client)

    extraction = await client.post(f"/documents/{document_id}/extractions")
    assert extraction.status_code == 201
    extraction_id = extraction.json()["extraction_id"]

    normalization = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}/normalizations"
    )
    assert normalization.status_code == 201
    assert normalization.json()["status"] == "COMPLETED"


async def test_stage_2_and_3_endpoints_unaffected(client: AsyncClient) -> None:
    document_id = await _upload(client)
    await client.post(f"/documents/{document_id}/pipeline")

    listing = await client.get("/documents")
    assert listing.status_code == 200
    assert any(d["document_id"] == document_id for d in listing.json())

    file_resp = await client.get(f"/documents/{document_id}/file")
    assert file_resp.status_code == 200
    assert file_resp.headers["content-type"] == "application/pdf"
