"""Tests for the decision API endpoints (Stage 6, package 5).

These drive the real HTTP layer through the ``client`` fixture. Extraction runs
on the deterministic offline fake and normalization/validation are fully
deterministic, so a completed validation is reached with three real HTTP calls
against the per-stage endpoints (not the pipeline, which would auto-decide it).
A technical decision failure is simulated by monkeypatching the ``decide``
symbol the service calls, mirroring ``tests/test_decision_service.py``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.api.deps import get_extractor
from app.services.processing.extraction.fake import FakeExtractionProvider

from tests._helpers import make_pdf

_DECIDE_PATH = "app.services.processing.decision.service.decide"
_VALIDATION_ENGINE_PATH = "app.services.processing.validation.service.evaluate"


@pytest.fixture(autouse=True)
def _offline_extractor(app):
    """Force the deterministic offline extractor for setup, regardless of the
    machine's ``EXTRACTION_PROVIDER``. Normalization, validation and decision
    have no provider at all."""
    app.dependency_overrides[get_extractor] = lambda: FakeExtractionProvider()
    yield


async def _upload(client: AsyncClient) -> str:
    resp = await client.post(
        "/documents", files={"file": ("invoice.pdf", make_pdf(1), "application/pdf")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["document_id"]


async def _completed_validation(
    client: AsyncClient,
) -> tuple[str, str, str, str]:
    """Upload, extract, normalize, validate via the per-stage endpoints.

    Return ``(doc_id, extraction_id, normalization_id, validation_id)``. The
    pipeline endpoint is deliberately avoided so the validation is left
    *undecided* for the decision endpoint under test.
    """
    document_id = await _upload(client)
    extraction = await client.post(f"/documents/{document_id}/extractions")
    assert extraction.status_code == 201, extraction.text
    extraction_id = extraction.json()["extraction_id"]

    normalization = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}/normalizations"
    )
    assert normalization.status_code == 201, normalization.text
    normalization_id = normalization.json()["normalization_id"]

    validation = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations"
    )
    assert validation.status_code == 201, validation.text
    assert validation.json()["status"] == "COMPLETED"
    return document_id, extraction_id, normalization_id, validation.json()["validation_id"]


def _base(document_id: str, extraction_id: str, normalization_id: str, validation_id: str) -> str:
    return (
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations/{validation_id}/decisions"
    )


async def _document_status(client: AsyncClient, document_id: str) -> str:
    resp = await client.get(f"/documents/{document_id}")
    assert resp.status_code == 200
    return resp.json()["status"]


# --- start -----------------------------------------------------------


async def test_start_returns_a_completed_accepted_result(client: AsyncClient) -> None:
    ids = await _completed_validation(client)

    resp = await client.post(_base(*ids))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["outcome"] == "ACCEPTED"
    assert body["attempt_number"] == 1
    assert body["validation_id"] == ids[3]
    assert body["policy_version"] == "1"
    assert body["failure_code"] is None and body["failure_message"] is None

    data = body["data"]
    assert set(data) == {"outcome", "reasons"}
    assert data["outcome"] == "ACCEPTED"
    # the deterministic fake invoice is clean - no findings, so no reasons
    assert data["reasons"] == []

    # nothing internal leaks
    assert "document_id" not in body
    assert "raw_response" not in resp.text

    # an ACCEPTED outcome leaves the document exactly where extraction left it
    assert await _document_status(client, ids[0]) == "COMPLETED"


async def test_start_unknown_links_are_404(client: AsyncClient) -> None:
    document_id, extraction_id, normalization_id, validation_id = await _completed_validation(
        client
    )

    bogus = str(uuid.uuid4())
    cases = {
        "DOCUMENT_NOT_FOUND": _base(bogus, extraction_id, normalization_id, validation_id),
        "EXTRACTION_NOT_FOUND": _base(document_id, bogus, normalization_id, validation_id),
        "NORMALIZATION_NOT_FOUND": _base(document_id, extraction_id, bogus, validation_id),
        "VALIDATION_NOT_FOUND": _base(document_id, extraction_id, normalization_id, bogus),
    }
    for expected_code, path in cases.items():
        resp = await client.post(path)
        assert resp.status_code == 404, (expected_code, resp.text)
        assert resp.json()["error"]["code"] == expected_code


@pytest.mark.parametrize("payload", [None, {}, {"manual_review_requested": False}])
async def test_start_accepts_an_empty_or_default_body(client: AsyncClient, payload) -> None:
    ids = await _completed_validation(client)
    resp = await client.post(_base(*ids), json=payload)
    assert resp.status_code == 201, resp.text
    assert resp.json()["outcome"] == "ACCEPTED"


async def test_start_rejects_unknown_body_keys(client: AsyncClient) -> None:
    ids = await _completed_validation(client)
    resp = await client.post(_base(*ids), json={"mode": "fast"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("bad", ["yes", 1, 0, "false", None])
async def test_start_manual_review_flag_is_strict(client: AsyncClient, bad) -> None:
    ids = await _completed_validation(client)
    resp = await client.post(_base(*ids), json={"manual_review_requested": bad})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_start_with_manual_review_requested_needs_review(client: AsyncClient) -> None:
    ids = await _completed_validation(client)

    resp = await client.post(_base(*ids), json={"manual_review_requested": True})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["outcome"] == "NEEDS_REVIEW"
    reasons = body["data"]["reasons"]
    assert reasons[-1]["code"] == "manual_review_requested"
    assert reasons[-1]["triggers_review"] is True
    assert reasons[-1]["source_rule"] is None

    # a NEEDS_REVIEW outcome moves the owning document
    assert await _document_status(client, ids[0]) == "NEEDS_REVIEW"


async def test_start_conflict_when_source_validation_not_completed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = await _upload(client)
    extraction = await client.post(f"/documents/{document_id}/extractions")
    extraction_id = extraction.json()["extraction_id"]
    normalization = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}/normalizations"
    )
    normalization_id = normalization.json()["normalization_id"]

    async def boom(_session, _normalization_id, *, started_at):
        raise RuntimeError("transient validation engine crash")

    monkeypatch.setattr(_VALIDATION_ENGINE_PATH, boom)
    failed = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations"
    )
    assert failed.json()["status"] == "FAILED"
    validation_id = failed.json()["validation_id"]

    resp = await client.post(
        _base(document_id, extraction_id, normalization_id, validation_id)
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VALIDATION_NOT_COMPLETED"


async def test_start_conflict_when_already_decided(client: AsyncClient) -> None:
    ids = await _completed_validation(client)
    assert (await client.post(_base(*ids))).status_code == 201

    resp = await client.post(_base(*ids))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VALIDATION_ALREADY_DECIDED"


async def test_start_technical_failure_is_201_with_a_safe_failed_body(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _completed_validation(client)

    def boom(*_args, **_kwargs):
        raise RuntimeError("engine crashed host=10.0.0.5 key=sk-super-secret")

    monkeypatch.setattr(_DECIDE_PATH, boom)

    resp = await client.post(_base(*ids))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["outcome"] is None
    assert body["data"] is None
    assert body["failure_code"] == "DECISION_FAILED"
    assert body["failure_message"] and "sk-super-secret" not in resp.text
    assert "10.0.0.5" not in resp.text
    # a technical failure is not a fact about the invoice
    assert await _document_status(client, ids[0]) == "COMPLETED"


# --- retry ---------------------------------------------------------


async def test_retry_conflict_when_decision_did_not_fail(client: AsyncClient) -> None:
    ids = await _completed_validation(client)

    resp = await client.post(_base(*ids) + "/retry")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DECISION_NOT_FAILED"


async def test_retry_after_a_technical_failure_completes_as_attempt_two(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _completed_validation(client)

    def boom(*_args, **_kwargs):
        raise RuntimeError("transient engine crash")

    monkeypatch.setattr(_DECIDE_PATH, boom)
    failed = await client.post(_base(*ids))
    assert failed.json()["status"] == "FAILED"

    monkeypatch.undo()
    resp = await client.post(_base(*ids) + "/retry")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["attempt_number"] == 2
    assert body["outcome"] == "ACCEPTED"


async def test_retry_carries_manual_review_request_on_the_new_attempt(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _completed_validation(client)

    def boom(*_args, **_kwargs):
        raise RuntimeError("transient engine crash")

    monkeypatch.setattr(_DECIDE_PATH, boom)
    assert (await client.post(_base(*ids))).json()["status"] == "FAILED"

    monkeypatch.undo()
    resp = await client.post(
        _base(*ids) + "/retry", json={"manual_review_requested": True}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["outcome"] == "NEEDS_REVIEW"
    assert body["data"]["reasons"][-1]["code"] == "manual_review_requested"


async def test_retry_unknown_validation_is_404(client: AsyncClient) -> None:
    document_id, extraction_id, normalization_id, _ = await _completed_validation(client)
    resp = await client.post(
        _base(document_id, extraction_id, normalization_id, str(uuid.uuid4())) + "/retry"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VALIDATION_NOT_FOUND"


# --- retrieval ---------------------------------------------------


async def test_latest_returns_the_newest_attempt(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _completed_validation(client)

    def boom(*_args, **_kwargs):
        raise RuntimeError("crash")

    monkeypatch.setattr(_DECIDE_PATH, boom)
    await client.post(_base(*ids))
    monkeypatch.undo()
    await client.post(_base(*ids) + "/retry")

    resp = await client.get(_base(*ids) + "/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["attempt_number"] == 2 and body["status"] == "COMPLETED"


async def test_latest_404_when_none_yet(client: AsyncClient) -> None:
    ids = await _completed_validation(client)
    resp = await client.get(_base(*ids) + "/latest")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DECISION_NOT_FOUND"


async def test_list_is_newest_first(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _completed_validation(client)

    def boom(*_args, **_kwargs):
        raise RuntimeError("crash")

    monkeypatch.setattr(_DECIDE_PATH, boom)
    await client.post(_base(*ids))
    monkeypatch.undo()
    await client.post(_base(*ids) + "/retry")

    resp = await client.get(_base(*ids))
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["attempt_number"] for r in rows] == [2, 1]
    assert [r["status"] for r in rows] == ["COMPLETED", "FAILED"]


async def test_list_is_empty_before_any_attempt(client: AsyncClient) -> None:
    ids = await _completed_validation(client)
    resp = await client.get(_base(*ids))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_specific_attempt_scoped_to_its_validation(client: AsyncClient) -> None:
    ids_a = await _completed_validation(client)
    ids_b = await _completed_validation(client)
    created = (await client.post(_base(*ids_a))).json()
    decision_id = created["decision_id"]

    ok = await client.get(f"{_base(*ids_a)}/{decision_id}")
    assert ok.status_code == 200
    assert ok.json()["decision_id"] == decision_id

    wrong_validation = await client.get(f"{_base(*ids_b)}/{decision_id}")
    assert wrong_validation.status_code == 404
    assert wrong_validation.json()["error"]["code"] == "DECISION_NOT_FOUND"

    unknown = await client.get(f"{_base(*ids_a)}/{uuid.uuid4()}")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "DECISION_NOT_FOUND"


async def test_retrieval_endpoints_404_for_unknown_document(client: AsyncClient) -> None:
    base = _base(*(str(uuid.uuid4()) for _ in range(4)))
    assert (await client.get(base)).status_code == 404
    assert (await client.get(base + "/latest")).status_code == 404
    assert (await client.get(f"{base}/{uuid.uuid4()}")).status_code == 404
    assert (await client.get(base)).json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


# --- Stage 5 stays intact ------------------------------------------


async def test_stage_5_validation_endpoints_still_work(client: AsyncClient) -> None:
    document_id, extraction_id, normalization_id, validation_id = await _completed_validation(
        client
    )

    latest = await client.get(
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations/latest"
    )
    assert latest.status_code == 200
    assert latest.json()["validation_id"] == validation_id
    assert "outcome" not in latest.json()
