"""Tests for the normalization API endpoints (Stage 4, step 12).

These drive the real HTTP layer through the ``client`` fixture. Extraction runs
on the deterministic offline fake; normalization has no provider at all. A
technical normalization failure is simulated by monkeypatching the engine
symbol the service calls.
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
    """Force the deterministic offline extractor for setup, regardless of the
    machine's ``EXTRACTION_PROVIDER``. Normalization itself has no provider."""
    app.dependency_overrides[get_extractor] = lambda: FakeExtractionProvider()
    yield


async def _upload(client: AsyncClient) -> str:
    resp = await client.post(
        "/documents", files={"file": ("invoice.pdf", make_pdf(1), "application/pdf")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["document_id"]


async def _completed_extraction(client: AsyncClient) -> tuple[str, str]:
    """Upload a PDF and run a deterministic extraction; return (doc_id, extraction_id)."""
    document_id = await _upload(client)
    resp = await client.post(f"/documents/{document_id}/extractions")
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "COMPLETED"
    return document_id, resp.json()["extraction_id"]


async def _failed_extraction(client: AsyncClient, app) -> tuple[str, str]:
    app.dependency_overrides[get_extractor] = lambda: FakeExtractionProvider(
        FakeBehavior.TIMEOUT
    )
    document_id = await _upload(client)
    resp = await client.post(f"/documents/{document_id}/extractions")
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "FAILED"
    app.dependency_overrides[get_extractor] = lambda: FakeExtractionProvider()
    return document_id, resp.json()["extraction_id"]


def _base(document_id: str, extraction_id: str) -> str:
    return f"/documents/{document_id}/extractions/{extraction_id}/normalizations"


# --- start -----------------------------------------------------------


async def test_start_returns_a_completed_result(client: AsyncClient) -> None:
    document_id, extraction_id = await _completed_extraction(client)

    resp = await client.post(_base(document_id, extraction_id))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["attempt_number"] == 1
    assert body["extraction_id"] == extraction_id
    assert body["failure_code"] is None and body["failure_message"] is None

    data = body["data"]
    assert set(data) >= {
        "invoice_number",
        "invoice_date",
        "currency",
        "total_amount",
        "line_items",
        "errors",
    }
    # canonical scalars, not {value, confidence} envelopes
    assert isinstance(data["invoice_number"], (str, type(None)))
    assert data["errors"] == []
    # nothing internal leaks
    assert "document_id" not in body
    assert "confidence" not in resp.text
    assert "raw_response" not in resp.text


async def test_start_unknown_document_is_404(client: AsyncClient) -> None:
    resp = await client.post(_base(str(uuid.uuid4()), str(uuid.uuid4())))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


async def test_start_unknown_extraction_is_404(client: AsyncClient) -> None:
    document_id = await _upload(client)
    resp = await client.post(_base(document_id, str(uuid.uuid4())))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "EXTRACTION_NOT_FOUND"


@pytest.mark.parametrize("payload", [None, {}])
async def test_start_accepts_an_empty_body(client: AsyncClient, payload) -> None:
    document_id, extraction_id = await _completed_extraction(client)
    resp = await client.post(_base(document_id, extraction_id), json=payload)
    assert resp.status_code == 201, resp.text


async def test_start_rejects_unknown_body_keys(client: AsyncClient) -> None:
    document_id, extraction_id = await _completed_extraction(client)
    resp = await client.post(_base(document_id, extraction_id), json={"mode": "fast"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_start_conflict_when_source_extraction_did_not_complete(
    client: AsyncClient, app
) -> None:
    document_id, extraction_id = await _failed_extraction(client, app)

    resp = await client.post(_base(document_id, extraction_id))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EXTRACTION_NOT_COMPLETED"


async def test_start_conflict_when_already_normalized(client: AsyncClient) -> None:
    document_id, extraction_id = await _completed_extraction(client)
    assert (await client.post(_base(document_id, extraction_id))).status_code == 201

    resp = await client.post(_base(document_id, extraction_id))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EXTRACTION_ALREADY_NORMALIZED"


async def test_start_technical_failure_is_201_with_a_safe_failed_body(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, extraction_id = await _completed_extraction(client)

    def boom(_contract):
        raise RuntimeError("engine crashed host=10.0.0.5 key=sk-super-secret")

    monkeypatch.setattr(_ENGINE_PATH, boom)

    resp = await client.post(_base(document_id, extraction_id))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["failure_code"] == "NORMALIZATION_FAILED"
    assert body["failure_message"] and "sk-super-secret" not in resp.text
    assert "10.0.0.5" not in resp.text
    assert body["data"]["invoice_number"] is None
    assert body["data"]["line_items"] == [] and body["data"]["errors"] == []


# --- retry ---------------------------------------------------------


async def test_retry_conflict_when_normalization_did_not_fail(
    client: AsyncClient,
) -> None:
    document_id, extraction_id = await _completed_extraction(client)

    resp = await client.post(_base(document_id, extraction_id) + "/retry")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NORMALIZATION_NOT_FAILED"


async def test_retry_after_a_technical_failure_completes_as_attempt_two(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, extraction_id = await _completed_extraction(client)

    def boom(_contract):
        raise RuntimeError("transient engine crash")

    monkeypatch.setattr(_ENGINE_PATH, boom)
    failed = await client.post(_base(document_id, extraction_id))
    assert failed.json()["status"] == "FAILED"

    monkeypatch.undo()
    resp = await client.post(_base(document_id, extraction_id) + "/retry")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["attempt_number"] == 2


async def test_retry_unknown_extraction_is_404(client: AsyncClient) -> None:
    document_id = await _upload(client)
    resp = await client.post(_base(document_id, str(uuid.uuid4())) + "/retry")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "EXTRACTION_NOT_FOUND"


# --- retrieval ---------------------------------------------------


async def test_latest_returns_the_newest_attempt(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, extraction_id = await _completed_extraction(client)

    def boom(_contract):
        raise RuntimeError("crash")

    monkeypatch.setattr(_ENGINE_PATH, boom)
    await client.post(_base(document_id, extraction_id))
    monkeypatch.undo()
    await client.post(_base(document_id, extraction_id) + "/retry")

    resp = await client.get(_base(document_id, extraction_id) + "/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["attempt_number"] == 2 and body["status"] == "COMPLETED"


async def test_latest_404_when_none_yet(client: AsyncClient) -> None:
    document_id, extraction_id = await _completed_extraction(client)
    resp = await client.get(_base(document_id, extraction_id) + "/latest")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NORMALIZATION_NOT_FOUND"


async def test_list_is_newest_first(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, extraction_id = await _completed_extraction(client)

    def boom(_contract):
        raise RuntimeError("crash")

    monkeypatch.setattr(_ENGINE_PATH, boom)
    await client.post(_base(document_id, extraction_id))
    monkeypatch.undo()
    await client.post(_base(document_id, extraction_id) + "/retry")

    resp = await client.get(_base(document_id, extraction_id))
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["attempt_number"] for r in rows] == [2, 1]
    assert [r["status"] for r in rows] == ["COMPLETED", "FAILED"]


async def test_list_is_empty_before_any_attempt(client: AsyncClient) -> None:
    document_id, extraction_id = await _completed_extraction(client)
    resp = await client.get(_base(document_id, extraction_id))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_specific_attempt_scoped_to_its_extraction(
    client: AsyncClient,
) -> None:
    doc_a, ext_a = await _completed_extraction(client)
    doc_b, ext_b = await _completed_extraction(client)
    created = (await client.post(_base(doc_a, ext_a))).json()
    normalization_id = created["normalization_id"]

    ok = await client.get(f"{_base(doc_a, ext_a)}/{normalization_id}")
    assert ok.status_code == 200
    assert ok.json()["normalization_id"] == normalization_id

    wrong_extraction = await client.get(f"{_base(doc_b, ext_b)}/{normalization_id}")
    assert wrong_extraction.status_code == 404
    assert wrong_extraction.json()["error"]["code"] == "NORMALIZATION_NOT_FOUND"

    unknown = await client.get(f"{_base(doc_a, ext_a)}/{uuid.uuid4()}")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "NORMALIZATION_NOT_FOUND"


async def test_retrieval_endpoints_404_for_unknown_document(
    client: AsyncClient,
) -> None:
    base = _base(str(uuid.uuid4()), str(uuid.uuid4()))
    assert (await client.get(base)).status_code == 404
    assert (await client.get(base + "/latest")).status_code == 404
    assert (await client.get(f"{base}/{uuid.uuid4()}")).status_code == 404
    assert (await client.get(base)).json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


# --- Stage 3 stays intact ------------------------------------------


async def test_stage_3_extraction_endpoints_still_work(client: AsyncClient) -> None:
    document_id, extraction_id = await _completed_extraction(client)

    latest = await client.get(f"/documents/{document_id}/extractions/latest")
    assert latest.status_code == 200
    assert latest.json()["extraction_id"] == extraction_id
