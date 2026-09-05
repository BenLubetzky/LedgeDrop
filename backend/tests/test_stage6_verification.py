"""Stage 6 verification suite (package 6).

One executable pass over the Stage 6 acceptance checklist in
``docs/stage-6-decision.md`` ("## Verification"). Where a bullet already has
exhaustive coverage elsewhere (the reason catalogue, the pure engine,
persistence, lifecycle, the service, and the API each have their own dense
test file from packages 1-5) this file exercises it *through the composed
stack* - upload -> extraction -> normalization -> validation -> decision -
so the engine, persistence, API, and pipeline are shown to hold together,
mirroring ``tests/test_stage5_verification.py``. It also fills the bullets
that had no automated test yet: an alembic upgrade/downgrade round trip for
``0005_decision_tables``, an end-to-end proof that a decision run leaves every
Stage 2-5 row and the stored PDF untouched and changes only the one
authorized ``documents.status`` field, and a "no AI / no network" guard for
the decision subsystem.

Checklist -> where it is proven
--------------------------------
* Clean invoice -> ACCEPTED, document stays COMPLETED
  ``test_clean_invoice_is_accepted_end_to_end`` (+ service-level
  ``test_decision_service.py::test_start_decides_a_clean_invoice_as_accepted``)
* Every one of the 15 rules -> its catalogued outcome, determinism, 1:1
  order-preserving reasons ...... ``test_decision_engine.py`` (an independent
  transcription of the §2.2 table); re-affirmed through the stack here by
  ``test_many_findings_produce_ordered_gated_reasons_end_to_end``
* Multiple findings -> every reason kept, Stage 5 order, no precedence
  ``test_many_findings_produce_ordered_gated_reasons_end_to_end``
* high_value_invoice elevated above its Stage 5 ``info`` severity (§2.2)
  ``test_high_value_clean_invoice_is_elevated_to_review_end_to_end``
* Duplicate invoice -> review .......... ``test_duplicate_invoice_is_routed_to_review_end_to_end``
* Unavailable per-field confidence (the real GPT-5-mini shape, every field
  ``null``) -> still ACCEPTED, reasons kept, non-gating (§2.3)
  ``test_all_null_confidence_invoice_is_still_accepted_end_to_end``
* Manual-review request -> adds one gating reason, appended last; add-only
  ``test_manual_review_request_adds_a_reason_end_to_end`` /
  ``test_manual_review_does_not_suppress_a_rule_reason``
* Upstream failure / not-yet-complete validation -> no decision row, pipeline
  ``decision`` is ``null`` (§2.5)
  ``test_pipeline_stops_before_decision_when_extraction_fails`` /
  ``test_pipeline_stops_before_decision_when_validation_fails`` /
  ``test_decision_start_rejected_when_source_validation_not_completed``
* Technical decision failure -> FAILED attempt, safe message, retryable, no
  partial reasons, document untouched
  ``test_technical_failure_is_safe_and_retryable_end_to_end``
* Retry a technical failure -> attempt 2; a COMPLETED decision is terminal
  ``test_technical_failure_is_safe_and_retryable_end_to_end`` /
  ``test_a_completed_decision_is_terminal``
* One active attempt per validation; concurrent starts -> exactly one wins
  ``test_concurrent_decision_starts_at_the_api_level_only_one_wins`` (+
  ``test_decision_service.py::test_two_concurrent_starts_only_one_wins``)
* Stale-source guard fires when the source chain is superseded (§6.4)
  ``test_a_superseded_validation_chain_is_rejected_as_stale``
* ``policy_version`` stamped; ``triggers_review`` stored per reason, not
  re-derived .......... ``test_clean_invoice_is_accepted_end_to_end`` /
  ``test_many_findings_produce_ordered_gated_reasons_end_to_end``
* Every Stage 2-5 row + the stored PDF unchanged after a decision run; only
  ``documents.status`` may change, and only to ``NEEDS_REVIEW``
  ``test_accepted_decision_leaves_the_whole_chain_and_pdf_untouched`` /
  ``test_needs_review_decision_changes_only_the_document_status`` /
  ``test_technical_failure_is_safe_and_retryable_end_to_end``
* Retrieval + 404 / 409 across all five endpoints .... ``test_decisions_api.py``
  (26 tests covering every endpoint and its relevant error codes)
* DB relationships & constraints ........ ``test_decision_model.py``
* Migration upgrade / downgrade with Stage 2-5 data preserved
  ``test_migration_upgrade_downgrade_preserves_stage2_5_data`` (here - new)
* No AI / external-network call ......... ``test_decision_subsystem_source_has_no_ai_or_network_import`` /
  ``test_decide_makes_no_socket_connection`` /
  ``test_decision_schema_layer_imports_no_ai_sdk``
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api.deps import get_extractor
from app.models import (
    Document,
    ExtractionAttempt,
    ExtractionLineItem,
    ExtractionStatus,
    FindingSeverity,
    NormalizationAttempt,
    NormalizationStatus,
    ValidationAttempt,
    ValidationFindingRow,
    ValidationRule,
    ValidationStatus,
)
from app.models.decision import (
    DecisionAttempt,
    DecisionOutcome,
    DecisionReasonCode,
    DecisionReasonRow,
    DecisionStatus,
)
from app.schemas.decision_catalogue import POLICY_VERSION, REASON_POLICY_BY_RULE
from app.services.processing.decision.engine import decide

from tests._helpers import make_pdf

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_VALIDATION_ENGINE_PATH = "app.services.processing.validation.service.evaluate"
_DECIDE_PATH = "app.services.processing.decision.service.decide"

_STAGE2_5_TABLES = (
    "documents",
    "invoice_extractions",
    "invoice_line_items",
    "invoice_normalizations",
    "invoice_normalized_line_items",
    "invoice_normalization_errors",
    "invoice_validations",
    "invoice_validation_findings",
)


# --- contract helpers ---------------------------------------------------


def _pair(value=None, confidence=None) -> dict:
    return {"value": value, "confidence": confidence}


def _clean_payload() -> dict:
    """A schema-valid extraction payload that reconciles cleanly on every rule."""
    return {
        "invoice_number": _pair("INV-2026-001", "0.95"),
        "invoice_date": _pair("2026-03-01", "0.95"),
        "due_date": _pair("2026-03-15", "0.95"),
        "vendor_name": _pair("Acme GmbH", "0.95"),
        "vendor_tax_id": _pair("DE123456789", "0.95"),
        "customer_name": _pair("Beta Ltd", "0.95"),
        "currency": _pair("EUR", "0.95"),
        "subtotal": _pair("100.00", "0.95"),
        "tax_amount": _pair("19.00", "0.95"),
        "total_amount": _pair("119.00", "0.95"),
        "line_items": [
            {
                "description": _pair("Widget", "0.95"),
                "quantity": _pair("2", "0.95"),
                "unit_price": _pair("50.00", "0.95"),
                "line_total": _pair("100.00", "0.95"),
            }
        ],
    }


def _all_null_confidence(payload: dict) -> dict:
    """Strip every ``confidence`` to ``None`` - the real GPT-5-mini shape."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in payload.items()}
    for field in out.values():
        if isinstance(field, dict) and "confidence" in field:
            field["confidence"] = None
    out["line_items"] = [
        {k: {"value": v["value"], "confidence": None} for k, v in item.items()}
        for item in payload["line_items"]
    ]
    return out


def _high_value_clean_payload() -> dict:
    """Clean on every data rule, but a total well over the high-value threshold."""
    payload = _clean_payload()
    payload["subtotal"] = _pair("20000.00", "0.95")
    payload["tax_amount"] = _pair("0.00", "0.95")
    payload["total_amount"] = _pair("20000.00", "0.95")
    payload["line_items"] = [
        {
            "description": _pair("Annual retainer", "0.95"),
            "quantity": _pair("1", "0.95"),
            "unit_price": _pair("20000.00", "0.95"),
            "line_total": _pair("20000.00", "0.95"),
        }
    ]
    return payload


def _many_findings_payload() -> dict:
    """Fires eight Stage 5 rules at once (the ``test_stage5_verification`` spread)."""
    payload = _clean_payload()
    payload["invoice_number"] = _pair("INV-99", "0.40")  # low_confidence_critical_field
    payload["due_date"] = _pair("2026-01-10")  # due_date_before_invoice_date
    payload["vendor_name"] = _pair("x" * 300)  # -> Stage 4 text_too_long -> null
    payload["currency"] = _pair("EUR", None)  # critical_field_confidence_unavailable
    payload["total_amount"] = _pair("20000.00", "0.95")  # high_value + does not reconcile
    payload["line_items"] = []  # -> no_line_items
    return payload


class _ScriptedProvider:
    """An extraction provider that returns a caller-supplied payload."""

    name = "scripted-verification"

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def extract(self, _prepared) -> dict:
        return self._payload


class _FailingProvider:
    name = "failing-verification"

    async def extract(self, _prepared):
        raise RuntimeError("provider socket exploded: key=sk-secret")


@pytest.fixture(autouse=True)
def _offline_extractor(app):
    app.dependency_overrides[get_extractor] = lambda: _ScriptedProvider(_clean_payload())
    yield


def _use_provider(app, payload: dict) -> None:
    app.dependency_overrides[get_extractor] = lambda: _ScriptedProvider(payload)


async def _upload(client: AsyncClient) -> str:
    resp = await client.post(
        "/documents", files={"file": ("invoice.pdf", make_pdf(1), "application/pdf")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["document_id"]


async def _piped(client: AsyncClient, app, payload: dict, **body) -> dict:
    """Upload, then run the whole pipeline (extraction .. decision)."""
    _use_provider(app, payload)
    document_id = await _upload(client)
    resp = await client.post(
        f"/documents/{document_id}/pipeline", json=body or None
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _reach_completed_validation(
    client: AsyncClient, app, payload: dict
) -> tuple[str, str, str, str]:
    """Drive the per-stage endpoints to a COMPLETED validation, leaving it
    *undecided* (the pipeline would decide it automatically)."""
    _use_provider(app, payload)
    document_id = await _upload(client)
    extraction = (await client.post(f"/documents/{document_id}/extractions")).json()
    extraction_id = extraction["extraction_id"]
    normalization = (
        await client.post(
            f"/documents/{document_id}/extractions/{extraction_id}/normalizations"
        )
    ).json()
    normalization_id = normalization["normalization_id"]
    validation = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations"
    )
    assert validation.status_code == 201, validation.text
    assert validation.json()["status"] == "COMPLETED"
    return document_id, extraction_id, normalization_id, validation.json()["validation_id"]


def _decisions_base(document_id, extraction_id, normalization_id, validation_id) -> str:
    return (
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations/{validation_id}/decisions"
    )


async def _document_status(client: AsyncClient, document_id: str) -> str:
    resp = await client.get(f"/documents/{document_id}")
    assert resp.status_code == 200
    return resp.json()["status"]


# --- clean acceptance, end to end -------------------------------------


async def test_clean_invoice_is_accepted_end_to_end(client: AsyncClient, app) -> None:
    body = await _piped(client, app, _clean_payload())

    assert body["validation"]["status"] == "COMPLETED"
    assert body["validation"]["data"]["findings"] == []

    decision = body["decision"]
    assert decision["status"] == "COMPLETED"
    assert decision["outcome"] == "ACCEPTED"
    assert decision["attempt_number"] == 1
    assert decision["policy_version"] == POLICY_VERSION
    assert decision["failure_code"] is None and decision["failure_message"] is None
    assert decision["data"] == {"outcome": "ACCEPTED", "reasons": []}

    # ACCEPTED never moves the document - COMPLETED already means "extraction
    # finished", not "accepted" (spec §6.2).
    assert await _document_status(client, body["extraction"]["document_id"]) == "COMPLETED"


# --- many findings -> every reason kept, in order, correctly gated ------


async def test_many_findings_produce_ordered_gated_reasons_end_to_end(
    client: AsyncClient, app
) -> None:
    body = await _piped(client, app, _many_findings_payload())

    findings = body["validation"]["data"]["findings"]
    decision = body["decision"]
    reasons = decision["data"]["reasons"]

    # 1:1, order-preserving: one reason per finding, same order, nothing dropped.
    assert [r["code"] for r in reasons] == [f["rule"] for f in findings]
    assert len(reasons) == 8

    for finding, reason in zip(findings, reasons):
        rule = ValidationRule(finding["rule"])
        policy = REASON_POLICY_BY_RULE[rule]
        # triggers_review is the catalogued policy call, stored per reason.
        assert reason["triggers_review"] is policy.triggers_review
        # a rule-derived reason names its rule and reuses its message verbatim.
        assert reason["source_rule"] == finding["rule"]
        assert reason["message"] == finding["message"]
        assert reason["field_path"] == finding["field_path"]

    # at least one gating reason -> NEEDS_REVIEW; the non-gating ones
    # (critical_field_confidence_unavailable, no_line_items) are still listed.
    assert any(r["triggers_review"] for r in reasons)
    assert decision["outcome"] == "NEEDS_REVIEW"
    assert {r["code"] for r in reasons if not r["triggers_review"]} == {
        "critical_field_confidence_unavailable",
        "no_line_items",
    }
    assert await _document_status(client, body["extraction"]["document_id"]) == "NEEDS_REVIEW"


# --- high-value elevation above Stage 5 severity (§2.2) ----------------


async def test_high_value_clean_invoice_is_elevated_to_review_end_to_end(
    client: AsyncClient, app
) -> None:
    body = await _piped(client, app, _high_value_clean_payload())

    findings = body["validation"]["data"]["findings"]
    assert [f["rule"] for f in findings] == ["high_value_invoice"]
    assert findings[0]["severity"] == "info"  # Stage 5 still calls it info

    decision = body["decision"]
    assert [r["code"] for r in decision["data"]["reasons"]] == ["high_value_invoice"]
    assert decision["data"]["reasons"][0]["triggers_review"] is True  # elevated
    assert decision["outcome"] == "NEEDS_REVIEW"
    assert await _document_status(client, body["extraction"]["document_id"]) == "NEEDS_REVIEW"


# --- duplicate detection routed to review -----------------------------


async def test_duplicate_invoice_is_routed_to_review_end_to_end(
    client: AsyncClient, app
) -> None:
    payload = _clean_payload()
    payload["invoice_number"] = _pair("DUP-STAGE6", "0.95")

    first = await _piped(client, app, payload)
    assert first["validation"]["data"]["findings"] == []
    assert first["decision"]["outcome"] == "ACCEPTED"

    second = await _piped(client, app, payload)  # identical invoice, new document
    dup = [
        f
        for f in second["validation"]["data"]["findings"]
        if f["rule"] == "probable_duplicate_invoice"
    ]
    assert len(dup) == 1
    codes = [r["code"] for r in second["decision"]["data"]["reasons"]]
    assert "probable_duplicate_invoice" in codes
    assert second["decision"]["outcome"] == "NEEDS_REVIEW"
    assert await _document_status(client, second["extraction"]["document_id"]) == "NEEDS_REVIEW"

    # asymmetry: the first document was decided before the second existed and a
    # COMPLETED decision is terminal - it stays ACCEPTED.
    assert await _document_status(client, first["extraction"]["document_id"]) == "COMPLETED"


# --- unavailable confidence, the real provider's stored shape (§2.3) ---


async def test_all_null_confidence_invoice_is_still_accepted_end_to_end(
    client: AsyncClient, app
) -> None:
    body = await _piped(client, app, _all_null_confidence(_clean_payload()))

    findings = body["validation"]["data"]["findings"]
    # every finding is the info-severity "confidence unavailable" rule
    assert {f["rule"] for f in findings} == {"critical_field_confidence_unavailable"}
    assert findings  # there is at least one - the critical fields

    decision = body["decision"]
    reasons = decision["data"]["reasons"]
    assert len(reasons) == len(findings)
    assert all(r["code"] == "critical_field_confidence_unavailable" for r in reasons)
    assert all(r["triggers_review"] is False for r in reasons)
    # unavailable confidence never gates and is never read as "high" - ACCEPTED.
    assert decision["outcome"] == "ACCEPTED"
    assert await _document_status(client, body["extraction"]["document_id"]) == "COMPLETED"


# --- manual-review request ------------------------------------------


async def test_manual_review_request_adds_a_reason_end_to_end(
    client: AsyncClient, app
) -> None:
    body = await _piped(client, app, _clean_payload(), manual_review_requested=True)

    decision = body["decision"]
    reasons = decision["data"]["reasons"]
    # a clean invoice, so the only reason is the appended manual one, last.
    assert [r["code"] for r in reasons] == ["manual_review_requested"]
    assert reasons[-1]["triggers_review"] is True
    assert reasons[-1]["source_rule"] is None
    assert reasons[-1]["field_path"] is None
    assert decision["outcome"] == "NEEDS_REVIEW"
    assert await _document_status(client, body["extraction"]["document_id"]) == "NEEDS_REVIEW"


async def test_manual_review_does_not_suppress_a_rule_reason(
    client: AsyncClient, app
) -> None:
    body = await _piped(
        client, app, _high_value_clean_payload(), manual_review_requested=True
    )
    codes = [r["code"] for r in body["decision"]["data"]["reasons"]]
    # add-only: the rule-derived reason survives and the manual one is appended.
    assert codes == ["high_value_invoice", "manual_review_requested"]
    assert body["decision"]["outcome"] == "NEEDS_REVIEW"


# --- upstream failure / not-yet-complete validation (§2.5) -----------


async def test_pipeline_stops_before_decision_when_extraction_fails(
    client: AsyncClient, app
) -> None:
    app.dependency_overrides[get_extractor] = lambda: _FailingProvider()
    document_id = await _upload(client)

    body = (await client.post(f"/documents/{document_id}/pipeline")).json()
    assert body["extraction"]["status"] == "FAILED"
    assert body["normalization"] is None
    assert body["validation"] is None
    assert body["decision"] is None
    assert "sk-secret" not in str(body)


async def test_pipeline_stops_before_decision_when_validation_fails(
    client: AsyncClient, app, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(_session, _normalization_id, *, started_at):
        raise RuntimeError("validation engine crash key=sk-secret")

    monkeypatch.setattr(_VALIDATION_ENGINE_PATH, boom)
    body = await _piped(client, app, _clean_payload())

    assert body["validation"]["status"] == "FAILED"
    assert body["decision"] is None
    assert "sk-secret" not in str(body)


async def test_decision_start_rejected_when_source_validation_not_completed(
    client: AsyncClient, app, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_provider(app, _clean_payload())
    document_id = await _upload(client)
    extraction = (await client.post(f"/documents/{document_id}/extractions")).json()
    extraction_id = extraction["extraction_id"]
    normalization = (
        await client.post(
            f"/documents/{document_id}/extractions/{extraction_id}/normalizations"
        )
    ).json()
    normalization_id = normalization["normalization_id"]

    async def boom(_session, _normalization_id, *, started_at):
        raise RuntimeError("transient validation engine crash")

    monkeypatch.setattr(_VALIDATION_ENGINE_PATH, boom)
    failed = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations"
    )
    assert failed.json()["status"] == "FAILED"
    validation_id = failed.json()["validation_id"]

    base = _decisions_base(document_id, extraction_id, normalization_id, validation_id)
    resp = await client.post(base)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VALIDATION_NOT_COMPLETED"
    # no decision row is fabricated for an unusable source
    assert (await client.get(base)).json() == []


# --- technical failure: safe, retryable, no partial state -------------


async def test_technical_failure_is_safe_and_retryable_end_to_end(
    client: AsyncClient, app, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _reach_completed_validation(client, app, _clean_payload())
    base = _decisions_base(*ids)

    def boom(*_a, **_k):
        raise RuntimeError("engine blew up host=10.0.0.9 key=sk-secret")

    monkeypatch.setattr(_DECIDE_PATH, boom)
    failed = await client.post(base)

    assert failed.status_code == 201, failed.text
    fbody = failed.json()
    assert fbody["status"] == "FAILED"
    assert fbody["outcome"] is None and fbody["data"] is None
    assert fbody["failure_code"] == "DECISION_FAILED"
    assert fbody["failure_message"] and "sk-secret" not in failed.text
    assert "10.0.0.9" not in failed.text
    # a technical failure is not a fact about the invoice
    assert await _document_status(client, ids[0]) == "COMPLETED"

    monkeypatch.undo()
    retry = await client.post(base + "/retry")
    assert retry.status_code == 201, retry.text
    assert retry.json()["status"] == "COMPLETED"
    assert retry.json()["attempt_number"] == 2
    assert retry.json()["outcome"] == "ACCEPTED"

    history = (await client.get(base)).json()
    assert [(r["attempt_number"], r["status"]) for r in history] == [
        (2, "COMPLETED"),
        (1, "FAILED"),
    ]
    # the failed attempt is untouched by the retry and holds no reasons
    failed_row = next(r for r in history if r["attempt_number"] == 1)
    assert failed_row["failure_code"] == "DECISION_FAILED"
    assert failed_row["data"] is None


async def test_a_completed_decision_is_terminal(client: AsyncClient, app) -> None:
    ids = await _reach_completed_validation(client, app, _clean_payload())
    base = _decisions_base(*ids)

    assert (await client.post(base)).status_code == 201
    again = await client.post(base)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "VALIDATION_ALREADY_DECIDED"


# --- concurrency at the real HTTP/API layer --------------------------


async def test_concurrent_decision_starts_at_the_api_level_only_one_wins(
    client: AsyncClient, app
) -> None:
    ids = await _reach_completed_validation(client, app, _clean_payload())
    base = _decisions_base(*ids)

    responses = await asyncio.gather(client.post(base), client.post(base))
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201, 409]
    conflict = next(r for r in responses if r.status_code == 409)
    assert conflict.json()["error"]["code"] in {
        "DECISION_IN_PROGRESS",
        "VALIDATION_ALREADY_DECIDED",
    }

    listing = await client.get(base)
    assert [row["attempt_number"] for row in listing.json()] == [1]
    assert listing.json()[0]["status"] == "COMPLETED"


# --- stale-source protection (§6.4) ---------------------------------


async def test_a_superseded_validation_chain_is_rejected_as_stale(
    client: AsyncClient, app, db_session: AsyncSession
) -> None:
    document_id, extraction_id, normalization_id, validation_id = (
        await _reach_completed_validation(client, app, _clean_payload())
    )

    # Manufacture a second, newer COMPLETED extraction for the same document,
    # directly against the database - not reachable through the API today
    # (extraction only retries a FAILED attempt), but exactly the shape a
    # future re-extraction feature would produce, and what this guard exists
    # to catch.
    await db_session.rollback()
    db_session.add(
        ExtractionAttempt(
            document_id=uuid.UUID(document_id),
            attempt_number=2,
            status=ExtractionStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            provider_name="fake",
        )
    )
    await db_session.commit()

    base = _decisions_base(document_id, extraction_id, normalization_id, validation_id)
    resp = await client.post(base)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "STALE_VALIDATION_SOURCE"
    assert (await client.get(base)).json() == []  # no decision row created


# --- source immutability + authorized status field only --------------


async def _snapshot_tables(
    conn: AsyncConnection | AsyncSession, tables: tuple[str, ...]
) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for table in tables:
        result = await conn.execute(sa.text(f'SELECT * FROM "{table}" ORDER BY 1'))
        out[table] = [dict(row) for row in result.mappings().all()]
    return out


async def test_accepted_decision_leaves_the_whole_chain_and_pdf_untouched(
    client: AsyncClient, app, db_session: AsyncSession
) -> None:
    ids = await _reach_completed_validation(client, app, _clean_payload())
    base = _decisions_base(*ids)

    await db_session.rollback()
    before = await _snapshot_tables(db_session, _STAGE2_5_TABLES)
    before_pdf = (await client.get(f"/documents/{ids[0]}/file")).content

    decided = await client.post(base)
    assert decided.status_code == 201 and decided.json()["outcome"] == "ACCEPTED"

    await db_session.rollback()
    after = await _snapshot_tables(db_session, _STAGE2_5_TABLES)
    after_pdf = (await client.get(f"/documents/{ids[0]}/file")).content

    assert after == before  # every Stage 2-5 row, incl. the documents row, byte-for-byte
    assert after_pdf == before_pdf


async def test_needs_review_decision_changes_only_the_document_status(
    client: AsyncClient, app, db_session: AsyncSession
) -> None:
    ids = await _reach_completed_validation(client, app, _high_value_clean_payload())
    base = _decisions_base(*ids)

    await db_session.rollback()
    before = await _snapshot_tables(db_session, _STAGE2_5_TABLES)
    before_pdf = (await client.get(f"/documents/{ids[0]}/file")).content

    decided = await client.post(base)
    assert decided.status_code == 201 and decided.json()["outcome"] == "NEEDS_REVIEW"

    await db_session.rollback()
    after = await _snapshot_tables(db_session, _STAGE2_5_TABLES)
    after_pdf = (await client.get(f"/documents/{ids[0]}/file")).content

    # Everything downstream of the documents row is untouched.
    for table in _STAGE2_5_TABLES:
        if table == "documents":
            continue
        assert after[table] == before[table], f"{table} changed"

    # The documents row changed in exactly two columns: status and updated_at.
    (before_doc,) = before["documents"]
    (after_doc,) = after["documents"]
    changed = {k for k in before_doc if before_doc[k] != after_doc[k]}
    assert changed == {"status", "updated_at"}
    assert before_doc["status"] == "COMPLETED"
    assert after_doc["status"] == "NEEDS_REVIEW"

    assert after_pdf == before_pdf


# --- no AI, no network --------------------------------------------


def test_decide_makes_no_socket_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    from app.schemas.validation import InvoiceValidation, ValidationFinding

    def _forbidden(*_a, **_k):
        raise AssertionError("deciding attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)

    validation = InvoiceValidation.from_findings(
        [
            ValidationFinding(
                rule=ValidationRule.HIGH_VALUE_INVOICE,
                severity=FindingSeverity.INFO,
                field_path=None,
                expected=None,
                actual=None,
                message="The invoice total is unusually high.",
                context={},
            ),
            ValidationFinding(
                rule=ValidationRule.NO_LINE_ITEMS,
                severity=FindingSeverity.INFO,
                field_path=None,
                expected=None,
                actual=None,
                message="The invoice has no line items.",
                context={},
            ),
        ]
    )

    decision = decide(validation, manual_review_requested=True)
    assert decision.outcome is DecisionOutcome.NEEDS_REVIEW
    assert [r.code.value for r in decision.reasons] == [
        "high_value_invoice",
        "no_line_items",
        "manual_review_requested",
    ]


_NETWORK_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+(openai|anthropic|httpx|requests|urllib|http\.client|aiohttp|socket)\b",
    re.MULTILINE,
)


def test_decision_subsystem_source_has_no_ai_or_network_import() -> None:
    """No file under the decision package imports an AI or network client.

    The engine and catalogue are pure; the lifecycle/service reuse the Stage
    3-5 repositories for data access only - pure DB readers with no network of
    their own.
    """
    roots = (
        _BACKEND_DIR / "app" / "services" / "processing" / "decision",
        _BACKEND_DIR / "app" / "schemas" / "decision.py",
        _BACKEND_DIR / "app" / "schemas" / "decision_catalogue.py",
        _BACKEND_DIR / "app" / "schemas" / "decision_persistence.py",
        _BACKEND_DIR / "app" / "schemas" / "decision_api.py",
        _BACKEND_DIR / "app" / "api" / "decisions.py",
    )
    files: list[Path] = []
    for root in roots:
        files.extend(root.rglob("*.py") if root.is_dir() else [root])

    offenders = {
        path.relative_to(_BACKEND_DIR).as_posix(): _NETWORK_IMPORT_RE.findall(
            path.read_text(encoding="utf-8")
        )
        for path in files
        if _NETWORK_IMPORT_RE.search(path.read_text(encoding="utf-8"))
    }
    assert offenders == {}, offenders


def test_decision_schema_layer_imports_no_ai_sdk() -> None:
    """Importing only the Stage 6 schema/contract/policy layer pulls in no AI SDK.

    The engine (``decide``) is equally pure - proven at the source level by
    ``test_decision_subsystem_source_has_no_ai_or_network_import`` and
    behaviourally by ``test_decide_makes_no_socket_connection``. It is not
    imported here by dotted path because the ``decision`` package ``__init__``
    also binds the lifecycle service, which reuses the Stage 3-5 repositories
    (pure DB readers) whose package, in turn, transitively imports the real
    provider SDK - the same documented caveat Stage 4's equivalent test carries.
    """
    probe = (
        "import sys; "
        "import app.schemas.decision; "
        "import app.schemas.decision_catalogue; "
        "import app.schemas.decision_persistence; "
        "import app.schemas.decision_api; "
        "leaked = sorted(m for m in ('openai', 'anthropic') if m in sys.modules); "
        "print(','.join(leaked)); "
        "sys.exit(1 if leaked else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(_BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"AI SDK imported by the Stage 6 schema layer: {result.stdout.strip()}\n{result.stderr}"
    )


# --- migration upgrade / downgrade, Stage 2-5 data preserved ---------


def _migration_database_url() -> str:
    from app.core.config import settings

    url = sa.engine.make_url(settings.database_url)
    name = f"ledgerdrop_stage6_migration_{uuid.uuid4().hex}"
    return url.set(database=name).render_as_string(hide_password=False)


async def _set_database(url: str, *, exists: bool) -> None:
    target = sa.engine.make_url(url)
    admin_url = target.set(database="postgres").render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(
                sa.text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)')
            )
            if exists:
                await conn.execute(sa.text(f'CREATE DATABASE "{target.database}"'))
    finally:
        await admin_engine.dispose()


def _run_alembic(*args: str, database_url: str) -> None:
    env = {**os.environ, "DATABASE_URL": database_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(_BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


_SNAPSHOT_TABLES = (*_STAGE2_5_TABLES, "invoice_decisions", "invoice_decision_reasons")


async def _snapshot(database_url: str) -> dict[str, list[dict]]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return await _snapshot_tables(conn, _SNAPSHOT_TABLES)
    finally:
        await engine.dispose()


async def _seed_full_chain_with_a_decision(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            doc = Document(
                original_filename="invoice.pdf",
                file_location=f"{uuid.uuid4()}/original.pdf",
                file_hash="a" * 64,
                file_size_bytes=2048,
                page_count=1,
            )
            session.add(doc)
            await session.flush()

            extraction = ExtractionAttempt(
                document_id=doc.document_id,
                attempt_number=1,
                status=ExtractionStatus.COMPLETED,
                completed_at=datetime.now(timezone.utc),
                provider_name="fake",
                invoice_number_value="INV-1",
                total_amount_value=Decimal("119.00"),
            )
            session.add(extraction)
            await session.flush()
            session.add(
                ExtractionLineItem(
                    extraction_id=extraction.extraction_id,
                    position=0,
                    description_value="Widget",
                    quantity_value=Decimal("2"),
                    unit_price_value=Decimal("50.00"),
                    line_total_value=Decimal("100.00"),
                )
            )

            normalization = NormalizationAttempt(
                extraction_id=extraction.extraction_id,
                attempt_number=1,
                status=NormalizationStatus.COMPLETED,
                completed_at=datetime.now(timezone.utc),
                invoice_number="INV-1",
                invoice_date="2026-03-01",
                vendor_name="Acme GmbH",
                currency="EUR",
                total_amount=Decimal("119.00"),
            )
            session.add(normalization)
            await session.flush()

            validation = ValidationAttempt(
                normalization_id=normalization.normalization_id,
                attempt_number=1,
                status=ValidationStatus.COMPLETED,
                completed_at=datetime.now(timezone.utc),
            )
            session.add(validation)
            await session.flush()
            finding = ValidationFindingRow(
                validation_id=validation.validation_id,
                position=0,
                rule=ValidationRule.NO_LINE_ITEMS,
                severity=FindingSeverity.INFO,
                field_path=None,
                expected=None,
                actual=None,
                message="The invoice has no line items.",
                context={},
            )
            session.add(finding)
            await session.flush()

            decision = DecisionAttempt(
                validation_id=validation.validation_id,
                attempt_number=1,
                status=DecisionStatus.COMPLETED,
                outcome=DecisionOutcome.ACCEPTED,
                policy_version=POLICY_VERSION,
                completed_at=datetime.now(timezone.utc),
            )
            decision.reasons = [
                DecisionReasonRow(
                    position=0,
                    code=DecisionReasonCode.NO_LINE_ITEMS,
                    triggers_review=False,
                    source_rule="no_line_items",
                    source_finding_id=finding.validation_finding_id,
                    field_path=None,
                    message="The invoice has no line items.",
                )
            ]
            session.add(decision)
            await session.commit()
    finally:
        await engine.dispose()


async def test_migration_upgrade_downgrade_preserves_stage2_5_data() -> None:
    """Downgrading + re-upgrading ``0005_decision_tables`` round-trips every
    Stage 2-5 row byte-for-byte and leaves ``alembic check`` clean at head.

    Runs against a dedicated throwaway database, driven by the real ``alembic``
    CLI so the actual migration files are exercised, not the ORM's own
    ``create_all``. Mirrors
    ``test_stage5_verification.py::test_migration_upgrade_downgrade_preserves_stage2_4_data``.
    """
    database_url = _migration_database_url()
    await _set_database(database_url, exists=True)
    try:
        _run_alembic("upgrade", "head", database_url=database_url)
        await _seed_full_chain_with_a_decision(database_url)

        before = await _snapshot(database_url)
        assert before["documents"] and before["invoice_extractions"]
        assert before["invoice_validations"] and before["invoice_validation_findings"]
        assert len(before["invoice_decisions"]) == 1
        assert len(before["invoice_decision_reasons"]) == 1

        _run_alembic("downgrade", "-1", database_url=database_url)
        _run_alembic("upgrade", "head", database_url=database_url)
        _run_alembic("check", database_url=database_url)

        after = await _snapshot(database_url)

        for table in _STAGE2_5_TABLES:
            assert after[table] == before[table], f"{table} changed across downgrade/upgrade"

        # the Stage 6 tables themselves were dropped and recreated empty by the
        # cycle - only Stage 2-5 rows must survive.
        assert after["invoice_decisions"] == []
        assert after["invoice_decision_reasons"] == []
    finally:
        await _set_database(database_url, exists=False)
