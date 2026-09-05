"""Tests for the Stage 5 validation engine (step 9).

Database-backed (the ``db_session`` fixture): they seed a document / extraction /
normalization chain, run ``evaluate`` over it, and check the assembled
``InvoiceValidation`` - including the confidence read, the §2.5 candidate query,
the ``started_at`` run-date dependence, and that nothing is written.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Document,
    ExtractionAttempt,
    ExtractionStatus,
    NormalizationAttempt,
    NormalizationFieldError,
    NormalizationLineItem,
    NormalizationStatus,
    ValidationAttempt,
)
from app.schemas.validation import InvoiceValidation, ValidationRule
from app.services.processing.validation import engine

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

_NORM_DEFAULTS: dict = {
    "invoice_number": "INV-1",
    "invoice_date": "2026-01-15",
    "due_date": "2026-02-14",
    "vendor_name": "Acme GmbH",
    "vendor_tax_id": "DE123456789",
    "customer_name": "Buyer Ltd",
    "currency": "EUR",
    "subtotal": Decimal("100.00"),
    "tax_amount": Decimal("19.00"),
    "total_amount": Decimal("119.00"),
}
_FULL_CONFIDENCE = {f"{n}_confidence": Decimal("0.99") for n in engine.policy.CRITICAL_FIELDS}


async def _chain(
    session: AsyncSession,
    *,
    ext_attempt: int = 1,
    ext_status: ExtractionStatus = ExtractionStatus.COMPLETED,
    norm_attempt: int = 1,
    norm_status: NormalizationStatus = NormalizationStatus.COMPLETED,
    norm_completed_at: datetime | None = None,
    confidence: dict | None = None,
    line_items: list[dict] | None = None,
    errors: list[dict] | None = None,
    document: Document | None = None,
    **norm_overrides,
) -> NormalizationAttempt:
    if document is None:
        document = Document(
            original_filename="invoice.pdf",
            file_location=f"{uuid.uuid4()}/original.pdf",
            file_hash="a" * 64,
            file_size_bytes=1234,
            page_count=1,
        )
        session.add(document)
        await session.flush()
    extraction = ExtractionAttempt(
        document_id=document.document_id,
        attempt_number=ext_attempt,
        status=ext_status,
        completed_at=NOW if ext_status is ExtractionStatus.COMPLETED else None,
        provider_name="fake",
        **({} if ext_status is not ExtractionStatus.COMPLETED else (confidence or _FULL_CONFIDENCE)),
    )
    session.add(extraction)
    await session.flush()
    norm_kwargs = {**_NORM_DEFAULTS, **norm_overrides}
    completed = norm_completed_at
    if completed is None and norm_status is not NormalizationStatus.PROCESSING:
        completed = NOW - timedelta(days=1)
    attempt = NormalizationAttempt(
        extraction_id=extraction.extraction_id,
        attempt_number=norm_attempt,
        status=norm_status,
        completed_at=completed,
        **(
            {"failure_code": "X", "failure_message": "x"}
            if norm_status is NormalizationStatus.FAILED
            else {}
        ),
        **norm_kwargs,
    )
    attempt.line_items = [
        NormalizationLineItem(position=i, **row) for i, row in enumerate(line_items or [])
    ]
    attempt.errors = [NormalizationFieldError(**row) for row in (errors or [])]
    session.add(attempt)
    await session.flush()
    return attempt


# --- assembly + contract validity ---------------------------------


async def test_evaluate_assembles_findings_and_a_valid_payload(
    db_session: AsyncSession,
) -> None:
    attempt = await _chain(
        db_session,
        invoice_number=None,  # missing required
        total_amount=Decimal("130.00"),  # does not reconcile (100 + 19)
        line_items=[],
    )
    await db_session.commit()

    result = await engine.evaluate(db_session, attempt.normalization_id, started_at=NOW)

    assert isinstance(result, InvoiceValidation)
    fired = {f.rule for f in result.findings}
    assert ValidationRule.MISSING_REQUIRED_FIELD in fired
    assert ValidationRule.TOTALS_DO_NOT_RECONCILE in fired
    assert ValidationRule.NO_LINE_ITEMS in fired
    assert result.summary.total == len(result.findings)
    # re-validates and round-trips
    assert (
        InvoiceValidation.model_validate_json(result.model_dump_json()).model_dump_json()
        == result.model_dump_json()
    )


async def test_evaluate_loads_the_attempt_errors_itself(
    db_session: AsyncSession,
) -> None:
    # the caller passes only an id; the engine loads line items + errors
    attempt = await _chain(
        db_session,
        subtotal=None,
        line_items=[],
        errors=[
            {
                "field_path": "subtotal",
                "raw_value": "x",
                "code": "ambiguous_number",
                "message": "bad",
            }
        ],
    )
    await db_session.commit()

    result = await engine.evaluate(db_session, attempt.normalization_id, started_at=NOW)
    assert any(
        f.rule is ValidationRule.NORMALIZATION_ERROR and f.field_path == "subtotal"
        for f in result.findings
    )


# --- confidence read (§2.6) --------------------------------------


async def test_evaluate_reads_confidence_from_the_source_extraction(
    db_session: AsyncSession,
) -> None:
    conf = {
        **_FULL_CONFIDENCE,
        "vendor_name_confidence": Decimal("0.40"),  # -> low_confidence
        "currency_confidence": None,  # -> unavailable
    }
    attempt = await _chain(db_session, confidence=conf, line_items=[])
    await db_session.commit()

    result = await engine.evaluate(db_session, attempt.normalization_id, started_at=NOW)
    by_rule = {(f.rule, f.field_path) for f in result.findings}
    assert (ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD, "vendor_name") in by_rule
    assert (
        ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE,
        "currency",
    ) in by_rule
    # a field with real, high confidence produces neither
    assert (ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD, "total_amount") not in by_rule
    assert (
        ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE,
        "total_amount",
    ) not in by_rule


async def test_evaluate_handles_an_all_null_confidence_row(
    db_session: AsyncSession,
) -> None:
    conf = {f"{n}_confidence": None for n in engine.policy.CRITICAL_FIELDS}
    attempt = await _chain(db_session, confidence=conf, line_items=[])
    await db_session.commit()

    result = await engine.evaluate(db_session, attempt.normalization_id, started_at=NOW)
    unavailable = [
        f
        for f in result.findings
        if f.rule is ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE
    ]
    assert {f.field_path for f in unavailable} == set(engine.policy.CRITICAL_FIELDS)
    assert not [
        f
        for f in result.findings
        if f.rule is ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD
    ]


# --- run-date dependence (§2.8) --------------------------------


async def test_evaluate_uses_started_at_as_the_run_date(
    db_session: AsyncSession,
) -> None:
    attempt = await _chain(db_session, invoice_date="2026-06-15", line_items=[])
    await db_session.commit()

    future_run = await engine.evaluate(db_session, attempt.normalization_id, started_at=NOW)
    assert any(
        f.rule is ValidationRule.INVOICE_DATE_IN_FUTURE for f in future_run.findings
    )

    later = NOW + timedelta(days=30)
    ok_run = await engine.evaluate(db_session, attempt.normalization_id, started_at=later)
    assert not any(
        f.rule is ValidationRule.INVOICE_DATE_IN_FUTURE for f in ok_run.findings
    )


def test_as_utc_date_helper() -> None:
    # a naive datetime is read as UTC; an aware one is converted first
    assert engine._as_utc_date(datetime(2026, 6, 1, 23, 0)) == date(2026, 6, 1)
    assert (
        engine._as_utc_date(
            datetime(2026, 6, 1, 2, 0, tzinfo=timezone(timedelta(hours=5)))
        )
        == date(2026, 5, 31)  # 02:00+05:00 -> 21:00 UTC previous day
    )


# --- duplicate candidate query (§2.5) -------------------------


async def test_duplicate_candidate_matched_across_documents(
    db_session: AsyncSession,
) -> None:
    # document B, normalized earlier, carries the same duplicate key
    await _chain(db_session, norm_completed_at=NOW - timedelta(days=2))
    attempt_a = await _chain(db_session, line_items=[])
    await db_session.commit()

    result = await engine.evaluate(db_session, attempt_a.normalization_id, started_at=NOW)
    dup = [
        f
        for f in result.findings
        if f.rule is ValidationRule.PROBABLE_DUPLICATE_INVOICE
    ]
    assert len(dup) == 1
    assert len(dup[0].context["matches"]) == 1


async def test_duplicate_candidate_excludes_the_current_document(
    db_session: AsyncSession,
) -> None:
    # `doc` has two completed, key-identical normalizations (two extractions);
    # validating one must not flag the other - the whole document is excluded.
    doc = Document(
        original_filename="i.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="b" * 64,
        file_size_bytes=1,
        page_count=1,
    )
    db_session.add(doc)
    await db_session.flush()
    await _chain(db_session, document=doc, ext_attempt=1)
    second = await _chain(db_session, document=doc, ext_attempt=2)
    await db_session.commit()

    result = await engine.evaluate(db_session, second.normalization_id, started_at=NOW)
    assert not any(
        f.rule is ValidationRule.PROBABLE_DUPLICATE_INVOICE for f in result.findings
    )


async def test_duplicate_candidate_no_fallback_to_older_extraction(
    db_session: AsyncSession,
) -> None:
    other = Document(
        original_filename="o.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="c" * 64,
        file_size_bytes=1,
        page_count=1,
    )
    db_session.add(other)
    await db_session.flush()
    # ext #1 completed + normalized (matching); ext #2 completed but NOT normalized
    await _chain(db_session, document=other, ext_attempt=1, norm_attempt=1)
    ext2 = ExtractionAttempt(
        document_id=other.document_id,
        attempt_number=2,
        status=ExtractionStatus.COMPLETED,
        completed_at=NOW,
        provider_name="fake",
    )
    db_session.add(ext2)

    attempt = await _chain(db_session, line_items=[])
    await db_session.commit()

    result = await engine.evaluate(db_session, attempt.normalization_id, started_at=NOW)
    assert not any(
        f.rule is ValidationRule.PROBABLE_DUPLICATE_INVOICE for f in result.findings
    )


async def test_duplicate_candidate_ignores_extraction_completed_after_cutoff(
    db_session: AsyncSession,
) -> None:
    other = Document(
        original_filename="o.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="e" * 64,
        file_size_bytes=1,
        page_count=1,
    )
    db_session.add(other)
    await db_session.flush()
    # At the validation cutoff, extraction #1 is the latest completed source
    # and has a matching normalization. Extraction #2 completes later and must
    # not displace it from the historical candidate snapshot.
    await _chain(
        db_session,
        document=other,
        ext_attempt=1,
        norm_completed_at=NOW - timedelta(days=1),
    )
    later_extraction = ExtractionAttempt(
        document_id=other.document_id,
        attempt_number=2,
        status=ExtractionStatus.COMPLETED,
        completed_at=NOW + timedelta(minutes=1),
        provider_name="fake",
    )
    db_session.add(later_extraction)

    attempt = await _chain(db_session, line_items=[])
    await db_session.commit()

    result = await engine.evaluate(db_session, attempt.normalization_id, started_at=NOW)
    assert any(
        f.rule is ValidationRule.PROBABLE_DUPLICATE_INVOICE for f in result.findings
    )


async def test_duplicate_candidate_as_of_cutoff(db_session: AsyncSession) -> None:
    # candidate normalized *after* the validation started -> excluded
    await _chain(db_session, norm_completed_at=NOW + timedelta(minutes=1))
    attempt = await _chain(db_session, line_items=[])
    await db_session.commit()

    result = await engine.evaluate(db_session, attempt.normalization_id, started_at=NOW)
    assert not any(
        f.rule is ValidationRule.PROBABLE_DUPLICATE_INVOICE for f in result.findings
    )


async def test_duplicate_candidate_uses_latest_normalization_attempt(
    db_session: AsyncSession,
) -> None:
    other = Document(
        original_filename="o.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="d" * 64,
        file_size_bytes=1,
        page_count=1,
    )
    db_session.add(other)
    await db_session.flush()
    ext = ExtractionAttempt(
        document_id=other.document_id,
        attempt_number=1,
        status=ExtractionStatus.COMPLETED,
        completed_at=NOW,
        provider_name="fake",
    )
    db_session.add(ext)
    await db_session.flush()
    # norm #1 completed, non-matching invoice_number; norm #2 completed, matching
    for n, inv_no in ((1, "OTHER"), (2, "INV-1")):
        db_session.add(
            NormalizationAttempt(
                extraction_id=ext.extraction_id,
                attempt_number=n,
                status=NormalizationStatus.COMPLETED,
                completed_at=NOW - timedelta(days=1),
                **{**_NORM_DEFAULTS, "invoice_number": inv_no},
            )
        )
    attempt = await _chain(db_session, line_items=[])
    await db_session.commit()

    result = await engine.evaluate(db_session, attempt.normalization_id, started_at=NOW)
    assert any(
        f.rule is ValidationRule.PROBABLE_DUPLICATE_INVOICE for f in result.findings
    )


async def test_no_duplicate_finding_when_alone(db_session: AsyncSession) -> None:
    attempt = await _chain(db_session, line_items=[])
    await db_session.commit()
    result = await engine.evaluate(db_session, attempt.normalization_id, started_at=NOW)
    assert not any(
        f.rule is ValidationRule.PROBABLE_DUPLICATE_INVOICE for f in result.findings
    )


# --- the engine writes nothing --------------------------------


async def test_evaluate_writes_nothing(db_session: AsyncSession) -> None:
    attempt = await _chain(
        db_session, total_amount=Decimal("130.00"), line_items=[]
    )
    await db_session.commit()

    before = {
        table: (
            await db_session.execute(select(func.count()).select_from(table))
        ).scalar_one()
        for table in (
            Document,
            ExtractionAttempt,
            NormalizationAttempt,
            NormalizationLineItem,
        )
    }
    norm_updated_at = (
        await db_session.execute(
            select(NormalizationAttempt.updated_at).where(
                NormalizationAttempt.normalization_id == attempt.normalization_id
            )
        )
    ).scalar_one()

    await engine.evaluate(db_session, attempt.normalization_id, started_at=NOW)
    await db_session.commit()

    after = {
        table: (
            await db_session.execute(select(func.count()).select_from(table))
        ).scalar_one()
        for table in before
    }
    assert after == before
    assert (
        await db_session.execute(select(func.count()).select_from(ValidationAttempt))
    ).scalar_one() == 0
    assert (
        await db_session.execute(
            select(NormalizationAttempt.updated_at).where(
                NormalizationAttempt.normalization_id == attempt.normalization_id
            )
        )
    ).scalar_one() == norm_updated_at
