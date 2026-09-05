"""Tests for the validation API endpoints (Stage 5, step 12).

These drive the real HTTP layer through the ``client`` fixture. Extraction
runs on the deterministic offline fake and normalization is fully
deterministic, so a completed normalization is reached via two real HTTP
calls. A technical validation failure is simulated by monkeypatching the
engine symbol the service calls, mirroring
``tests/test_normalizations_api.py``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.api.deps import get_extractor
from app.services.processing.extraction.fake import FakeExtractionProvider

from tests._helpers import make_pdf

_VALIDATION_ENGINE_PATH = "app.services.processing.validation.service.evaluate"
_NORMALIZATION_ENGINE_PATH = (
    "app.services.processing.normalization.service.normalize_extraction"
)


@pytest.fixture(autouse=True)
def _offline_extractor(app):
    """Force the deterministic offline extractor for setup, regardless of the
    machine's ``EXTRACTION_PROVIDER``. Normalization and validation have no
    provider at all."""
    app.dependency_overrides[get_extractor] = lambda: FakeExtractionProvider()
    yield


async def _upload(client: AsyncClient) -> str:
    resp = await client.post(
        "/documents", files={"file": ("invoice.pdf", make_pdf(1), "application/pdf")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["document_id"]


async def _completed_normalization(client: AsyncClient) -> tuple[str, str, str]:
    """Upload, extract, and normalize; return (doc_id, extraction_id, normalization_id)."""
    document_id = await _upload(client)
    extraction = await client.post(f"/documents/{document_id}/extractions")
    assert extraction.status_code == 201, extraction.text
    assert extraction.json()["status"] == "COMPLETED"
    extraction_id = extraction.json()["extraction_id"]

    normalization = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}/normalizations"
    )
    assert normalization.status_code == 201, normalization.text
    assert normalization.json()["status"] == "COMPLETED"
    return document_id, extraction_id, normalization.json()["normalization_id"]


def _base(document_id: str, extraction_id: str, normalization_id: str) -> str:
    return (
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations"
    )


# --- start -----------------------------------------------------------


async def test_start_returns_a_completed_result(client: AsyncClient) -> None:
    document_id, extraction_id, normalization_id = await _completed_normalization(client)

    resp = await client.post(_base(document_id, extraction_id, normalization_id))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["attempt_number"] == 1
    assert body["normalization_id"] == normalization_id
    assert body["failure_code"] is None and body["failure_message"] is None

    data = body["data"]
    assert set(data) == {"findings", "summary"}
    assert data["summary"]["total"] == len(data["findings"])
    # nothing internal or from a later decision stage leaks
    assert "document_id" not in body
    assert "verdict" not in resp.text
    assert "NEEDS_REVIEW" not in resp.text


async def test_start_unknown_document_is_404(client: AsyncClient) -> None:
    resp = await client.post(
        _base(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


async def test_start_unknown_extraction_is_404(client: AsyncClient) -> None:
    document_id = await _upload(client)
    resp = await client.post(
        _base(document_id, str(uuid.uuid4()), str(uuid.uuid4()))
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "EXTRACTION_NOT_FOUND"


async def test_start_unknown_normalization_is_404(client: AsyncClient) -> None:
    document_id = await _upload(client)
    extraction = await client.post(f"/documents/{document_id}/extractions")
    extraction_id = extraction.json()["extraction_id"]

    resp = await client.post(
        _base(document_id, extraction_id, str(uuid.uuid4()))
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NORMALIZATION_NOT_FOUND"


@pytest.mark.parametrize("payload", [None, {}])
async def test_start_accepts_an_empty_body(client: AsyncClient, payload) -> None:
    document_id, extraction_id, normalization_id = await _completed_normalization(client)
    resp = await client.post(
        _base(document_id, extraction_id, normalization_id), json=payload
    )
    assert resp.status_code == 201, resp.text


async def test_start_rejects_unknown_body_keys(client: AsyncClient) -> None:
    document_id, extraction_id, normalization_id = await _completed_normalization(client)
    resp = await client.post(
        _base(document_id, extraction_id, normalization_id), json={"mode": "fast"}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_start_conflict_when_source_normalization_did_not_complete(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = await _upload(client)
    extraction = await client.post(f"/documents/{document_id}/extractions")
    extraction_id = extraction.json()["extraction_id"]

    def boom(_contract):
        raise RuntimeError("transient engine crash")

    monkeypatch.setattr(_NORMALIZATION_ENGINE_PATH, boom)
    failed = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}/normalizations"
    )
    assert failed.json()["status"] == "FAILED"
    normalization_id = failed.json()["normalization_id"]

    resp = await client.post(_base(document_id, extraction_id, normalization_id))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NORMALIZATION_NOT_COMPLETED"


async def test_start_conflict_when_already_validated(client: AsyncClient) -> None:
    document_id, extraction_id, normalization_id = await _completed_normalization(client)
    assert (
        await client.post(_base(document_id, extraction_id, normalization_id))
    ).status_code == 201

    resp = await client.post(_base(document_id, extraction_id, normalization_id))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NORMALIZATION_ALREADY_VALIDATED"


async def test_start_technical_failure_is_201_with_a_safe_failed_body(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, extraction_id, normalization_id = await _completed_normalization(client)

    async def boom(_session, _normalization_id, *, started_at):
        raise RuntimeError("engine crashed host=10.0.0.5 key=sk-super-secret")

    monkeypatch.setattr(_VALIDATION_ENGINE_PATH, boom)

    resp = await client.post(_base(document_id, extraction_id, normalization_id))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["failure_code"] == "VALIDATION_FAILED"
    assert body["failure_message"] and "sk-super-secret" not in resp.text
    assert "10.0.0.5" not in resp.text
    assert body["data"]["findings"] == [] and body["data"]["summary"]["total"] == 0


# --- retry ---------------------------------------------------------


async def test_retry_conflict_when_validation_did_not_fail(
    client: AsyncClient,
) -> None:
    document_id, extraction_id, normalization_id = await _completed_normalization(client)

    resp = await client.post(_base(document_id, extraction_id, normalization_id) + "/retry")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VALIDATION_NOT_FAILED"


async def test_retry_after_a_technical_failure_completes_as_attempt_two(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, extraction_id, normalization_id = await _completed_normalization(client)

    async def boom(_session, _normalization_id, *, started_at):
        raise RuntimeError("transient engine crash")

    monkeypatch.setattr(_VALIDATION_ENGINE_PATH, boom)
    failed = await client.post(_base(document_id, extraction_id, normalization_id))
    assert failed.json()["status"] == "FAILED"

    monkeypatch.undo()
    resp = await client.post(_base(document_id, extraction_id, normalization_id) + "/retry")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["attempt_number"] == 2


async def test_retry_unknown_normalization_is_404(client: AsyncClient) -> None:
    document_id = await _upload(client)
    extraction = await client.post(f"/documents/{document_id}/extractions")
    extraction_id = extraction.json()["extraction_id"]

    resp = await client.post(
        _base(document_id, extraction_id, str(uuid.uuid4())) + "/retry"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NORMALIZATION_NOT_FOUND"


# --- retrieval ---------------------------------------------------


async def test_latest_returns_the_newest_attempt(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, extraction_id, normalization_id = await _completed_normalization(client)

    async def boom(_session, _normalization_id, *, started_at):
        raise RuntimeError("crash")

    monkeypatch.setattr(_VALIDATION_ENGINE_PATH, boom)
    await client.post(_base(document_id, extraction_id, normalization_id))
    monkeypatch.undo()
    await client.post(_base(document_id, extraction_id, normalization_id) + "/retry")

    resp = await client.get(_base(document_id, extraction_id, normalization_id) + "/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["attempt_number"] == 2 and body["status"] == "COMPLETED"


async def test_latest_404_when_none_yet(client: AsyncClient) -> None:
    document_id, extraction_id, normalization_id = await _completed_normalization(client)
    resp = await client.get(_base(document_id, extraction_id, normalization_id) + "/latest")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VALIDATION_NOT_FOUND"


async def test_list_is_newest_first(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, extraction_id, normalization_id = await _completed_normalization(client)

    async def boom(_session, _normalization_id, *, started_at):
        raise RuntimeError("crash")

    monkeypatch.setattr(_VALIDATION_ENGINE_PATH, boom)
    await client.post(_base(document_id, extraction_id, normalization_id))
    monkeypatch.undo()
    await client.post(_base(document_id, extraction_id, normalization_id) + "/retry")

    resp = await client.get(_base(document_id, extraction_id, normalization_id))
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["attempt_number"] for r in rows] == [2, 1]
    assert [r["status"] for r in rows] == ["COMPLETED", "FAILED"]


async def test_list_is_empty_before_any_attempt(client: AsyncClient) -> None:
    document_id, extraction_id, normalization_id = await _completed_normalization(client)
    resp = await client.get(_base(document_id, extraction_id, normalization_id))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_specific_attempt_scoped_to_its_normalization(
    client: AsyncClient,
) -> None:
    doc_a, ext_a, norm_a = await _completed_normalization(client)
    doc_b, ext_b, norm_b = await _completed_normalization(client)
    created = (await client.post(_base(doc_a, ext_a, norm_a))).json()
    validation_id = created["validation_id"]

    ok = await client.get(f"{_base(doc_a, ext_a, norm_a)}/{validation_id}")
    assert ok.status_code == 200
    assert ok.json()["validation_id"] == validation_id

    wrong_normalization = await client.get(
        f"{_base(doc_b, ext_b, norm_b)}/{validation_id}"
    )
    assert wrong_normalization.status_code == 404
    assert wrong_normalization.json()["error"]["code"] == "VALIDATION_NOT_FOUND"

    unknown = await client.get(f"{_base(doc_a, ext_a, norm_a)}/{uuid.uuid4()}")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "VALIDATION_NOT_FOUND"


async def test_retrieval_endpoints_404_for_unknown_document(
    client: AsyncClient,
) -> None:
    base = _base(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
    assert (await client.get(base)).status_code == 404
    assert (await client.get(base + "/latest")).status_code == 404
    assert (await client.get(f"{base}/{uuid.uuid4()}")).status_code == 404
    assert (await client.get(base)).json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


# --- Stage 4 stays intact ------------------------------------------


async def test_stage_4_normalization_endpoints_still_work(client: AsyncClient) -> None:
    document_id, extraction_id, normalization_id = await _completed_normalization(client)

    latest = await client.get(
        f"/documents/{document_id}/extractions/{extraction_id}/normalizations/latest"
    )
    assert latest.status_code == 200
    assert latest.json()["normalization_id"] == normalization_id
