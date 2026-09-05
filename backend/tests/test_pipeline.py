"""Tests for the composed processing pipeline (Stage 4 step 13; Stage 5 step 13).

The pipeline chains ``ExtractionService``, ``NormalizationService``, and
``ValidationService`` against one session. These run at the service layer (via
``db_session``) with a plain callable extraction producer - no AI provider is
involved. The per-stage services are exercised directly too, to prove they
stay independently callable.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import (
    Document,
    DocumentStatus,
    ExtractionStatus,
    NormalizationAttempt,
    NormalizationStatus,
)
from app.models.validation import ValidationAttempt, ValidationStatus
from app.schemas.extraction import InvoiceExtraction
from app.services.processing.extraction import ExtractionService
from app.services.processing.normalization import NormalizationRepository, NormalizationService
from app.services.processing.pipeline import Action, ProcessingPipeline
from app.services.processing.validation import ValidationRepository, ValidationService

_NORMALIZATION_ENGINE_PATH = (
    "app.services.processing.normalization.service.normalize_extraction"
)
_VALIDATION_ENGINE_PATH = "app.services.processing.validation.service.evaluate"


def _field(value=None, confidence=None) -> dict:
    return {"value": value, "confidence": confidence}


def _contract() -> InvoiceExtraction:
    return InvoiceExtraction.model_validate(
        {
            "invoice_number": _field("INV-77"),
            "invoice_date": _field("3 March 2026"),
            "due_date": _field(None),
            "vendor_name": _field("Acme GmbH"),
            "vendor_tax_id": _field("DE 123 456 789"),
            "customer_name": _field("Beta Ltd"),
            "currency": _field("eur"),
            "subtotal": _field("100.00"),
            "tax_amount": _field("19.00"),
            "total_amount": _field("119.00"),
            "line_items": [
                {
                    "description": _field("Widget"),
                    "quantity": _field("2"),
                    "unit_price": _field("10.00"),
                    "line_total": _field("20.00"),
                }
            ],
        }
    )


async def _make_document(
    session: AsyncSession, *, status: DocumentStatus = DocumentStatus.UPLOADED
) -> Document:
    doc = Document(
        original_filename="invoice.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="a" * 64,
        file_size_bytes=2048,
        page_count=2,
        status=status,
    )
    session.add(doc)
    await session.flush()
    return doc


async def _ok(_doc: Document) -> InvoiceExtraction:
    return _contract()


async def _boom(_doc: Document) -> InvoiceExtraction:
    raise RuntimeError("provider socket exploded: key=sk-secret")


# --- happy path --------------------------------------------------------


async def test_run_chains_extraction_normalization_validation(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    document_id = doc.document_id

    result = await ProcessingPipeline(db_session).run(
        document_id, produce=_ok, provider_name="fake"
    )

    assert result.extraction.status is ExtractionStatus.COMPLETED
    assert result.normalization is not None
    assert result.normalization.status is NormalizationStatus.COMPLETED
    assert result.normalization.extraction_id == result.extraction.extraction_id
    assert result.normalization.attempt_number == 1
    assert result.normalization.invoice_date == "2026-03-03"
    assert result.normalization.total_amount == Decimal("119.00")
    assert [li.position for li in result.normalization.line_items] == [0]
    assert list(result.normalization.errors) == []

    assert result.validation is not None
    assert result.validation.status is ValidationStatus.COMPLETED
    assert result.validation.normalization_id == result.normalization.normalization_id
    assert result.validation.attempt_number == 1

    db_session.expire_all()
    reloaded_doc = await db_session.get(Document, document_id)
    assert reloaded_doc.status is DocumentStatus.COMPLETED


# --- extraction stops the chain -------------------------------------


async def test_run_stops_when_extraction_fails(db_session: AsyncSession) -> None:
    doc = await _make_document(db_session)

    result = await ProcessingPipeline(db_session).run(
        doc.document_id, produce=_boom, provider_name="fake"
    )

    assert result.extraction.status is ExtractionStatus.FAILED
    assert result.normalization is None
    assert result.validation is None

    count = await db_session.scalar(
        select(func.count()).select_from(NormalizationAttempt)
    )
    assert count == 0
    count = await db_session.scalar(select(func.count()).select_from(ValidationAttempt))
    assert count == 0


# --- normalization technical failure leaves the extraction intact ------


async def test_normalization_failure_does_not_undo_the_extraction(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = await _make_document(db_session)

    def engine_boom(_contract):
        raise RuntimeError("engine crash key=sk-secret")

    monkeypatch.setattr(_NORMALIZATION_ENGINE_PATH, engine_boom)

    result = await ProcessingPipeline(db_session).run(
        doc.document_id, produce=_ok, provider_name="fake"
    )

    assert result.extraction.status is ExtractionStatus.COMPLETED
    assert result.extraction.total_amount_value == Decimal("119.00")
    assert result.normalization is not None
    assert result.normalization.status is NormalizationStatus.FAILED
    assert result.normalization.failure_code == "NORMALIZATION_FAILED"
    assert "sk-secret" not in (result.normalization.failure_message or "")

    # No stable normalization to validate.
    assert result.validation is None
    count = await db_session.scalar(select(func.count()).select_from(ValidationAttempt))
    assert count == 0


# --- validation technical failure leaves normalization intact ----------


async def test_validation_failure_does_not_undo_the_normalization(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = await _make_document(db_session)

    async def engine_boom(_session, _normalization_id, *, started_at):
        raise RuntimeError("validation engine crash key=sk-secret")

    monkeypatch.setattr(_VALIDATION_ENGINE_PATH, engine_boom)

    result = await ProcessingPipeline(db_session).run(
        doc.document_id, produce=_ok, provider_name="fake"
    )

    assert result.extraction.status is ExtractionStatus.COMPLETED
    assert result.normalization is not None
    assert result.normalization.status is NormalizationStatus.COMPLETED
    assert result.validation is not None
    assert result.validation.status is ValidationStatus.FAILED
    assert result.validation.failure_code == "VALIDATION_FAILED"
    assert "sk-secret" not in (result.validation.failure_message or "")


# --- retry -----------------------------------------------------------


async def test_run_retry_reretries_extraction_then_normalizes_and_validates(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    pipeline = ProcessingPipeline(db_session)

    first = await pipeline.run(doc.document_id, produce=_boom, provider_name="fake")
    assert first.extraction.status is ExtractionStatus.FAILED
    assert first.normalization is None
    assert first.validation is None

    second = await pipeline.run(
        doc.document_id, action="retry", produce=_ok, provider_name="fake"
    )
    assert second.extraction.status is ExtractionStatus.COMPLETED
    assert second.extraction.attempt_number == 2
    assert second.normalization is not None
    assert second.normalization.status is NormalizationStatus.COMPLETED
    assert second.normalization.extraction_id == second.extraction.extraction_id
    assert second.validation is not None
    assert second.validation.status is ValidationStatus.COMPLETED
    assert second.validation.normalization_id == second.normalization.normalization_id


async def test_run_propagates_extraction_stage_conflicts(db_session: AsyncSession) -> None:
    doc = await _make_document(db_session)
    pipeline = ProcessingPipeline(db_session)
    await pipeline.run(doc.document_id, produce=_ok, provider_name="fake")

    with pytest.raises(ConflictError) as excinfo:
        await pipeline.run(doc.document_id, produce=_ok, provider_name="fake")
    assert excinfo.value.code == "DOCUMENT_ALREADY_EXTRACTED"


async def test_run_rejects_an_unknown_action_before_starting_work(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    document_id = doc.document_id

    with pytest.raises(ValueError, match="unknown pipeline action"):
        await ProcessingPipeline(db_session).run(
            document_id,
            action=cast(Action, "unknown"),
            produce=_ok,
            provider_name="fake",
        )

    db_session.expire_all()
    reloaded = await db_session.get(Document, document_id)
    assert reloaded.status is DocumentStatus.UPLOADED


# --- defensive: normalization already present -----------------------


async def test_continue_returns_existing_normalization_on_conflict(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    extraction = await ExtractionService(db_session).start(
        doc.document_id, produce=_ok, provider_name="fake"
    )
    # A normalization attempt started directly on its own service.
    existing = await NormalizationService(db_session).start(extraction.extraction_id)

    got = await ProcessingPipeline(db_session)._continue_to_normalization(extraction)

    assert got is not None
    assert got.normalization_id == existing.normalization_id
    # No second attempt was created.
    count = await db_session.scalar(
        select(func.count()).select_from(NormalizationAttempt)
    )
    assert count == 1


async def test_continue_does_not_hide_unrelated_normalization_conflicts(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = await _make_document(db_session)
    extraction = await ExtractionService(db_session).start(
        doc.document_id, produce=_ok, provider_name="fake"
    )
    pipeline = ProcessingPipeline(db_session)

    async def reject(_extraction_id):
        raise ConflictError("Unexpected lifecycle conflict.", code="UNEXPECTED_CONFLICT")

    monkeypatch.setattr(pipeline._normalization, "start", reject)

    with pytest.raises(ConflictError) as excinfo:
        await pipeline._continue_to_normalization(extraction)
    assert excinfo.value.code == "UNEXPECTED_CONFLICT"


# --- defensive: validation already present ---------------------------


async def test_continue_returns_existing_validation_on_conflict(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    extraction = await ExtractionService(db_session).start(
        doc.document_id, produce=_ok, provider_name="fake"
    )
    normalization = await NormalizationService(db_session).start(extraction.extraction_id)
    # A validation attempt started directly on its own service.
    existing = await ValidationService(db_session).start(normalization.normalization_id)

    got = await ProcessingPipeline(db_session)._continue_to_validation(normalization)

    assert got is not None
    assert got.validation_id == existing.validation_id
    # No second attempt was created.
    count = await db_session.scalar(select(func.count()).select_from(ValidationAttempt))
    assert count == 1


async def test_continue_does_not_hide_unrelated_validation_conflicts(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = await _make_document(db_session)
    extraction = await ExtractionService(db_session).start(
        doc.document_id, produce=_ok, provider_name="fake"
    )
    normalization = await NormalizationService(db_session).start(extraction.extraction_id)
    pipeline = ProcessingPipeline(db_session)

    async def reject(_normalization_id):
        raise ConflictError("Unexpected lifecycle conflict.", code="UNEXPECTED_CONFLICT")

    monkeypatch.setattr(pipeline._validation, "start", reject)

    with pytest.raises(ConflictError) as excinfo:
        await pipeline._continue_to_validation(normalization)
    assert excinfo.value.code == "UNEXPECTED_CONFLICT"


async def test_continue_to_validation_skips_a_missing_or_incomplete_normalization(
    db_session: AsyncSession,
) -> None:
    pipeline = ProcessingPipeline(db_session)
    assert await pipeline._continue_to_validation(None) is None


# --- stages remain independently callable --------------------------


async def test_stages_stay_independently_callable_alongside_the_pipeline(
    db_session: AsyncSession,
) -> None:
    piped = await _make_document(db_session)
    await ProcessingPipeline(db_session).run(
        piped.document_id, produce=_ok, provider_name="fake"
    )

    # A different document, driven one stage at a time through the plain services.
    solo = await _make_document(db_session)
    extraction = await ExtractionService(db_session).start(
        solo.document_id, produce=_ok, provider_name="fake"
    )
    assert extraction.status is ExtractionStatus.COMPLETED

    normalization = await NormalizationService(db_session).start(extraction.extraction_id)
    assert normalization.status is NormalizationStatus.COMPLETED

    repo = NormalizationRepository(db_session)
    assert (await repo.latest_for_extraction(extraction.extraction_id)) is not None

    validation = await ValidationService(db_session).start(normalization.normalization_id)
    assert validation.status is ValidationStatus.COMPLETED

    validation_repo = ValidationRepository(db_session)
    assert (
        await validation_repo.latest_for_normalization(normalization.normalization_id)
    ) is not None
