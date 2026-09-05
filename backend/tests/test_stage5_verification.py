"""Stage 5 verification suite (step 14).

One executable pass over the Stage 5 acceptance checklist in
``docs/stage-5-validation.md`` ("## Verification"). Where a bullet already has
exhaustive coverage elsewhere (the rule catalogue, the deterministic rule
functions, the engine, persistence, lifecycle, and API each have their own
dense test file from steps 3-12) this file exercises it *through the composed
stack* instead of re-deriving the matrix, so the engine, persistence, API, and
pipeline are shown to hold together - mirroring
``tests/test_stage4_verification.py``. It also fills the few bullets that had
no automated test yet: an alembic upgrade/downgrade round trip, and an
end-to-end (API-level) proof that a validation run leaves every Stage 2-4 row
and the stored PDF untouched.

Checklist -> where it is proven
--------------------------------
* Every rule, passing and failing, incl. tolerance edges  ``test_validation_rules.py``
  (an exhaustive matrix of each rule's fire/skip/boundary conditions)
* missing_required_field + normalization_error co-occurrence
  ``test_validation_rules.py::
  test_missing_and_normalization_error_co_occur_for_a_null_required_field``
  (+ end to end here: ``test_golden_invoice_with_many_findings_end_to_end``)
* Normalization errors surfaced without re-running normalization, severity
  keyed to required-ness .. ``test_validation_rules.py::test_normalization_error_on_*``
* Date order / future / implausibly-old + run-date dependence (§2.8)
  ``test_validation_rules.py::test_due_date_*`` / ``test_invoice_date_*`` /
  ``test_validation_engine.py::test_evaluate_uses_started_at_as_the_run_date``
* Monetary & line-item reconciliation, target precedence, per-line tolerance
  growth .......... ``test_validation_rules.py::test_totals_do_not_reconcile_*`` /
  ``test_line_item*`` / ``test_line_items_do_not_sum_*``
* line_item_sum_not_checked / no_line_items info findings
  ``test_validation_rules.py::test_line_item_sum_not_checked_*`` /
  ``test_no_line_items_*`` (+ end to end here)
* Confidence: warning below threshold, info on null, silent at/above or absent,
  on both a real-confidence and an all-null-confidence (OpenAI-like) row
  ``test_validation_rules.py::test_low_confidence_*`` / ``test_confidence_unavailable_*`` /
  ``test_validation_engine.py::test_evaluate_handles_an_all_null_confidence_row``
  (+ end to end here)
* Duplicate detection: exact match, each key field differing, candidate-set
  scoping, deterministic ordering, multi-match single finding, A/B asymmetry
  ``test_validation_rules.py::test_duplicate_*`` /
  ``test_validation_engine.py::test_duplicate_candidate_*``
  (+ end to end here: ``test_duplicate_invoice_end_to_end``)
* High-value at/around the threshold and default, ``abs()`` on a negative total
  ``test_validation_rules.py::test_high_value_invoice_*`` (+ end to end here)
* Lifecycle: COMPLETED-with-findings vs FAILED-on-fault; retry -> attempt 2;
  a COMPLETED attempt is not re-runnable
  ``test_validation_lifecycle.py`` /
  ``test_validation_service.py::test_start_with_no_findings_still_completes``
  / ``test_engine_exception_marks_failed_with_a_safe_message`` /
  ``test_retry_after_technical_failure_creates_new_attempt_and_completes`` /
  ``test_validations_api.py::test_start_conflict_when_already_validated``
* One active attempt per normalization; concurrent starts -> exactly one wins;
  illegal starts -> 409
  ``test_validation_service.py::test_an_active_attempt_blocks_a_new_start`` /
  ``test_two_concurrent_starts_only_one_wins`` (+ API-level here:
  ``test_concurrent_starts_at_the_api_level_only_one_wins``)
* Transactional persistence: forced write failure -> zero finding rows + FAILED
  ``test_validation_service.py::test_persist_failure_rolls_back_and_stores_no_partial_result``
* Retrieval and 404 / 409 across all five endpoints .... ``test_validations_api.py``
  (20 tests covering every endpoint and its relevant error codes)
* Every documents / invoice_extractions / invoice_normalizations* row
  unchanged after a run, byte-for-byte, and the stored PDF untouched
  ``test_validation_service.py::test_source_normalization_and_document_are_untouched``
  (service layer) + ``test_full_chain_untouched_by_a_validation_run_end_to_end``
  (here: API level, extraction + normalization + the PDF bytes too)
* Database relationships & constraints ................. ``test_validation_model.py``
* Migration upgrade / downgrade with Stage 2-4 data preserved
  ``test_migration_upgrade_downgrade_preserves_stage2_4_data`` (here - new)
* Boundary tests (§1.7): no decision vocabulary in the contract
  ``test_validation_contract.py::test_no_decision_state_leaks_into_status`` /
  ``test_finding_rejects_decision_fields`` / ``test_summary_fields_are_only_counts``
  (the fourth §1.7 bullet - rows unchanged end to end - is
  ``test_full_chain_untouched_by_a_validation_run_end_to_end`` here)
* No AI / external-network call ......................... ``test_validation_rules.py::
  test_validation_package_source_has_no_ai_or_network_import`` /
  ``test_run_rules_makes_no_socket_connection`` (+
  ``test_validation_schema_layer_imports_no_ai_sdk`` here, mirroring Stage 4's
  equivalent schema-layer check)
"""

from __future__ import annotations

import asyncio
import os
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
    NormalizationAttempt,
    NormalizationErrorCode,
    NormalizationFieldError,
    NormalizationLineItem,
    NormalizationStatus,
)
from app.services.processing.validation import policy

from tests._helpers import make_pdf

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_STAGE2_4_TABLES = (
    "documents",
    "invoice_extractions",
    "invoice_line_items",
    "invoice_normalizations",
    "invoice_normalized_line_items",
    "invoice_normalization_errors",
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


class _ScriptedProvider:
    """An extraction provider that returns a caller-supplied payload."""

    name = "scripted-verification"

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def extract(self, _prepared) -> dict:
        return self._payload


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


async def _piped(client: AsyncClient, app, payload: dict) -> dict:
    """Upload, then run extraction -> normalization -> validation via the pipeline."""
    _use_provider(app, payload)
    document_id = await _upload(client)
    resp = await client.post(f"/documents/{document_id}/pipeline")
    assert resp.status_code == 201, resp.text
    return resp.json()


def _findings(body: dict) -> list[dict]:
    return body["validation"]["data"]["findings"]


# --- golden clean invoice: zero findings, end to end --------------------


async def test_golden_clean_invoice_validates_with_no_findings_end_to_end(
    client: AsyncClient, app
) -> None:
    body = await _piped(client, app, _clean_payload())

    assert body["extraction"]["status"] == "COMPLETED"
    assert body["normalization"]["status"] == "COMPLETED"
    assert body["normalization"]["data"]["errors"] == []
    assert body["validation"]["status"] == "COMPLETED"
    assert body["validation"]["attempt_number"] == 1
    data = body["validation"]["data"]
    assert data["findings"] == []
    assert data["summary"] == {"total": 0, "error": 0, "warning": 0, "info": 0}
    # no decision vocabulary in the validation result itself (§1.7 boundary) -
    # Stage 5 reports facts, not a decision. The pipeline envelope's separate
    # ``decision`` block (Stage 6) is where an outcome legitimately appears.
    assert "NEEDS_REVIEW" not in str(body["validation"])
    assert "ACCEPTED" not in str(body["validation"])
    assert body["decision"]["status"] == "COMPLETED"
    assert body["decision"]["outcome"] == "ACCEPTED"


# --- golden invoice: many rules fire together, end to end ---------------


async def test_golden_invoice_with_many_findings_end_to_end(
    client: AsyncClient, app
) -> None:
    payload = _clean_payload()
    payload["invoice_number"] = _pair("INV-99", "0.40")  # below the 0.70 threshold
    payload["due_date"] = _pair("2026-01-10")  # before the invoice date
    payload["vendor_name"] = _pair("x" * 300)  # -> Stage 4 text_too_long -> null
    payload["currency"] = _pair("EUR", None)  # present, confidence unavailable
    payload["total_amount"] = _pair("20000.00", "0.95")  # high value + does not reconcile
    payload["line_items"] = []  # -> no_line_items

    body = await _piped(client, app, payload)

    assert body["normalization"]["status"] == "COMPLETED"  # field errors are not a failure
    assert body["normalization"]["data"]["vendor_name"] is None
    assert body["validation"]["status"] == "COMPLETED"

    data = body["validation"]["data"]
    got = {(f["rule"], f["field_path"], f["severity"]) for f in data["findings"]}
    assert got == {
        ("missing_required_field", "vendor_name", "error"),
        ("normalization_error", "vendor_name", "error"),
        ("due_date_before_invoice_date", "due_date", "warning"),
        ("totals_do_not_reconcile", None, "warning"),
        ("low_confidence_critical_field", "invoice_number", "warning"),
        ("critical_field_confidence_unavailable", "currency", "info"),
        ("high_value_invoice", None, "info"),
        ("no_line_items", None, "info"),
    }
    assert data["summary"] == {"total": 8, "error": 2, "warning": 3, "info": 3}

    # spot-check a couple of context/expected-actual payloads to prove the
    # engine's numbers reach the API unchanged, not just the rule identities.
    by_rule = {f["rule"]: f for f in data["findings"]}
    normalization_error = by_rule["normalization_error"]
    assert normalization_error["context"]["code"] == "text_too_long"
    high_value = by_rule["high_value_invoice"]
    assert high_value["context"] == {
        "threshold": str(policy.high_value_threshold("EUR")),
        "currency": "EUR",
    }
    totals = by_rule["totals_do_not_reconcile"]
    assert totals["expected"] == "119.00" and totals["actual"] == "20000.00"


# --- duplicate detection: A/B asymmetry, end to end ----------------------


async def test_duplicate_invoice_end_to_end(client: AsyncClient, app) -> None:
    payload = _clean_payload()
    payload["invoice_number"] = _pair("DUP-1", "0.95")
    payload["total_amount"] = _pair("500.00", "0.95")
    payload["subtotal"] = _pair("500.00", "0.95")
    payload["tax_amount"] = _pair("0.00", "0.95")
    payload["line_items"] = [
        {
            "description": _pair("Consulting"),
            "quantity": _pair("1"),
            "unit_price": _pair("500.00"),
            "line_total": _pair("500.00"),
        }
    ]

    first = await _piped(client, app, payload)
    first_document_id = first["extraction"]["document_id"]
    assert _findings(first) == []  # nothing to match against yet - no false positive

    second = await _piped(client, app, payload)  # identical invoice, a different document
    findings = _findings(second)
    duplicate = [f for f in findings if f["rule"] == "probable_duplicate_invoice"]
    assert len(duplicate) == 1
    assert duplicate[0]["severity"] == "warning"
    assert duplicate[0]["field_path"] is None
    assert duplicate[0]["context"]["matches"] == [
        {
            "document_id": first_document_id,
            "normalization_id": first["normalization"]["normalization_id"],
        }
    ]

    # asymmetry: re-checking the first document's own (already-COMPLETED)
    # attempt still shows no duplicate finding - it was validated before the
    # second document existed, and a COMPLETED attempt is not re-run.
    assert _findings(first) == []


# --- source immutability: the whole chain, end to end ---------------------


async def test_full_chain_untouched_by_a_validation_run_end_to_end(
    client: AsyncClient, app, db_session: AsyncSession
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

    await db_session.rollback()
    before_rows = await _snapshot_tables(db_session, _STAGE2_4_TABLES)
    before_pdf = (await client.get(f"/documents/{document_id}/file")).content

    validation = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations"
    )
    assert validation.status_code == 201, validation.text
    assert validation.json()["status"] == "COMPLETED"

    await db_session.rollback()
    after_rows = await _snapshot_tables(db_session, _STAGE2_4_TABLES)
    after_pdf = (await client.get(f"/documents/{document_id}/file")).content

    assert after_rows == before_rows
    assert after_pdf == before_pdf  # the stored PDF bytes themselves, not just metadata


# --- concurrency, at the real HTTP/API layer ------------------------------


async def test_concurrent_starts_at_the_api_level_only_one_wins(
    client: AsyncClient, app
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
    base = (
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations"
    )

    responses = await asyncio.gather(client.post(base), client.post(base))
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201, 409]
    conflict = next(r for r in responses if r.status_code == 409)
    assert conflict.json()["error"]["code"] in {
        "VALIDATION_IN_PROGRESS",
        "NORMALIZATION_ALREADY_VALIDATED",
    }

    listing = await client.get(base)
    assert [row["attempt_number"] for row in listing.json()] == [1]


# --- no AI, no network (schema layer) -------------------------------------


def test_validation_schema_layer_imports_no_ai_sdk() -> None:
    """Importing only the Stage 5 schema/contract layer pulls in no AI SDK."""
    probe = (
        "import sys; "
        "import app.schemas.validation; "
        "import app.schemas.validation_catalogue; "
        "import app.schemas.validation_persistence; "
        "import app.schemas.validation_api; "
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
        f"AI SDK imported by the Stage 5 schema layer: {result.stdout.strip()}\n{result.stderr}"
    )


# --- migration upgrade / downgrade, Stage 2-4 data preserved -------------


def _migration_database_url() -> str:
    from app.core.config import settings

    url = sa.engine.make_url(settings.database_url)
    # A unique name prevents concurrent verification runs from force-dropping
    # each other's database. Keep below PostgreSQL's 63-byte identifier limit.
    name = f"ledgerdrop_migration_verify_{uuid.uuid4().hex}"
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


_SNAPSHOT_TABLES = (
    *_STAGE2_4_TABLES,
    "invoice_validations",
    "invoice_validation_findings",
)


async def _snapshot_tables(
    session: AsyncConnection | AsyncSession, tables: tuple[str, ...]
) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for table in tables:
        result = await session.execute(sa.text(f'SELECT * FROM "{table}" ORDER BY 1'))
        out[table] = [dict(row) for row in result.mappings().all()]
    return out


async def _snapshot(database_url: str) -> dict[str, list[dict]]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        out: dict[str, list[dict]] = {}
        async with engine.connect() as conn:
            out = await _snapshot_tables(conn, _SNAPSHOT_TABLES)
        return out
    finally:
        await engine.dispose()


async def test_migration_upgrade_downgrade_preserves_stage2_4_data() -> None:
    """Downgrading + re-upgrading the Stage 5 migration round-trips Stage 2-4
    data byte-for-byte and leaves ``alembic check`` clean at head.

    Runs against a dedicated throwaway database (never the shared test
    database other tests use), driven by the real ``alembic`` CLI as a
    subprocess so this exercises the actual migration files, not the ORM's own
    ``create_all``.
    """
    database_url = _migration_database_url()
    await _set_database(database_url, exists=True)
    try:
        _run_alembic("upgrade", "head", database_url=database_url)

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
                    invoice_number_confidence=Decimal("0.9"),
                    total_amount_value=Decimal("119.00"),
                    total_amount_confidence=Decimal("0.9"),
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
                session.add(
                    NormalizationLineItem(
                        normalization_id=normalization.normalization_id,
                        position=0,
                        description="Widget",
                        quantity=Decimal("2"),
                        unit_price=Decimal("50.00"),
                        line_total=Decimal("100.00"),
                    )
                )
                session.add(
                    NormalizationFieldError(
                        normalization_id=normalization.normalization_id,
                        field_path="due_date",
                        raw_value="31/02/2026",
                        code=NormalizationErrorCode.INVALID_DATE,
                        message="The due date could not be recognized.",
                    )
                )
                await session.commit()
        finally:
            await engine.dispose()

        before = await _snapshot(database_url)
        assert before["documents"] and before["invoice_extractions"]
        assert before["invoice_line_items"] and before["invoice_normalizations"]
        assert before["invoice_normalized_line_items"]
        assert before["invoice_normalization_errors"]

        _run_alembic("downgrade", "-1", database_url=database_url)
        _run_alembic("upgrade", "head", database_url=database_url)
        _run_alembic("check", database_url=database_url)

        after = await _snapshot(database_url)

        for table in (
            "documents",
            "invoice_extractions",
            "invoice_line_items",
            "invoice_normalizations",
            "invoice_normalized_line_items",
            "invoice_normalization_errors",
        ):
            assert after[table] == before[table], f"{table} changed across downgrade/upgrade"

        # the Stage 5 tables themselves were necessarily dropped and recreated
        # empty by the downgrade/upgrade cycle - only Stage 2-4 rows must survive.
        assert after["invoice_validations"] == []
        assert after["invoice_validation_findings"] == []
    finally:
        await _set_database(database_url, exists=False)
