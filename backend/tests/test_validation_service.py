"""Tests for the validation repository/service, and lifecycle wiring
(Stage 5, step 11).

These run against the real PostgreSQL test database (via the ``db_session``
fixture). A completed source normalization is built directly from ORM rows (no
extraction/normalization service involved - Stage 5 only reads their output);
a technical failure is simulated by monkeypatching the engine's ``evaluate`` or
the repository's ``apply_result``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError, NotFoundError
from app.models import (
    Document,
    ExtractionAttempt,
    ExtractionStatus,
    NormalizationAttempt,
    NormalizationStatus,
    ValidationAttempt,
    ValidationFindingRow,
    ValidationStatus,
)
from app.schemas.validation import InvoiceValidation, ValidationRule
from app.services.processing.validation import lifecycle
from app.services.processing.validation.repository import ValidationRepository
from app.services.processing.validation.service import _GENERIC_FAILURE, ValidationService

_ENGINE_PATH = "app.services.processing.validation.service.evaluate"

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
    "total_amount": Decimal("130.00"),  # deliberately does not reconcile
}
_FULL_CONFIDENCE = {"invoice_number_confidence": Decimal("0.99"),
                     "invoice_date_confidence": Decimal("0.99"),
                     "vendor_name_confidence": Decimal("0.99"),
                     "currency_confidence": Decimal("0.99"),
                     "total_amount_confidence": Decimal("0.99")}


async def _completed_normalization(
    session: AsyncSession, **norm_overrides
) -> NormalizationAttempt:
    doc = Document(
        original_filename="invoice.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="a" * 64,
        file_size_bytes=1234,
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
        **_FULL_CONFIDENCE,
    )
    session.add(extraction)
    await session.flush()
    attempt = NormalizationAttempt(
        extraction_id=extraction.extraction_id,
        attempt_number=1,
        status=NormalizationStatus.COMPLETED,
        completed_at=datetime.now(timezone.utc),
        **{**_NORM_DEFAULTS, **norm_overrides},
    )
    session.add(attempt)
    await session.flush()
    await session.commit()
    return attempt


async def _failed_normalization(session: AsyncSession) -> NormalizationAttempt:
    doc = Document(
        original_filename="invoice.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="b" * 64,
        file_size_bytes=1234,
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
        **_FULL_CONFIDENCE,
    )
    session.add(extraction)
    await session.flush()
    attempt = NormalizationAttempt(
        extraction_id=extraction.extraction_id,
        attempt_number=1,
        status=NormalizationStatus.FAILED,
        completed_at=datetime.now(timezone.utc),
        failure_code="NORMALIZATION_FAILED",
        failure_message="boom",
    )
    session.add(attempt)
    await session.flush()
    await session.commit()
    return attempt


# --- happy path --------------------------------------------------------


async def test_start_runs_the_rules_and_persists_findings(
    db_session: AsyncSession,
) -> None:
    normalization = await _completed_normalization(db_session)

    attempt = await ValidationService(db_session).start(
        normalization.normalization_id
    )

    assert attempt.status is ValidationStatus.COMPLETED
    assert attempt.attempt_number == 1
    assert attempt.normalization_id == normalization.normalization_id
    assert attempt.started_at is not None and attempt.completed_at is not None
    assert attempt.failure_code is None and attempt.failure_message is None

    rules_fired = {f.rule for f in attempt.findings}
    assert ValidationRule.TOTALS_DO_NOT_RECONCILE in rules_fired  # 100+19 != 130
    assert ValidationRule.NO_LINE_ITEMS in rules_fired
    positions = [f.position for f in attempt.findings]
    assert positions == sorted(positions) == list(range(len(positions)))

    validation_id = attempt.validation_id
    db_session.expire_all()
    reloaded = await ValidationRepository(db_session).get(validation_id)
    assert reloaded.status is ValidationStatus.COMPLETED
    assert len(reloaded.findings) == len(attempt.findings)


async def test_start_with_no_findings_still_completes(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    normalization = await _completed_normalization(db_session)

    async def no_findings(*_args, **_kwargs) -> InvoiceValidation:
        return InvoiceValidation.from_findings([])

    monkeypatch.setattr(_ENGINE_PATH, no_findings)
    attempt = await ValidationService(db_session).start(
        normalization.normalization_id
    )
    assert attempt.status is ValidationStatus.COMPLETED
    assert list(attempt.findings) == []
    assert attempt.failure_code is None


# --- not found / conflicts ---------------------------------------------


async def test_start_on_unknown_normalization_raises_not_found(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(NotFoundError) as excinfo:
        await ValidationService(db_session).start(uuid.uuid4())
    assert excinfo.value.code == "NORMALIZATION_NOT_FOUND"


async def test_start_rejected_when_source_normalization_not_completed(
    db_session: AsyncSession,
) -> None:
    failed = await _failed_normalization(db_session)

    with pytest.raises(ConflictError) as excinfo:
        await ValidationService(db_session).start(failed.normalization_id)
    assert excinfo.value.code == "NORMALIZATION_NOT_COMPLETED"

    assert (
        await ValidationRepository(db_session).list_for_normalization(
            failed.normalization_id
        )
        == []
    )


async def test_start_rejected_when_already_validated(
    db_session: AsyncSession,
) -> None:
    normalization = await _completed_normalization(db_session)
    svc = ValidationService(db_session)

    first = await svc.start(normalization.normalization_id)
    assert first.status is ValidationStatus.COMPLETED

    with pytest.raises(ConflictError) as excinfo:
        await svc.start(normalization.normalization_id)
    assert excinfo.value.code == "NORMALIZATION_ALREADY_VALIDATED"


async def test_an_active_attempt_blocks_a_new_start(db_session: AsyncSession) -> None:
    normalization = await _completed_normalization(db_session)
    db_session.add(
        ValidationAttempt(
            normalization_id=normalization.normalization_id,
            attempt_number=1,
            status=ValidationStatus.PROCESSING,
        )
    )
    await db_session.commit()

    with pytest.raises(ConflictError) as excinfo:
        await ValidationService(db_session).start(normalization.normalization_id)
    assert excinfo.value.code == "VALIDATION_IN_PROGRESS"


async def test_retry_requires_a_technically_failed_attempt(
    db_session: AsyncSession,
) -> None:
    normalization = await _completed_normalization(db_session)
    svc = ValidationService(db_session)

    with pytest.raises(ConflictError) as excinfo:
        await svc.retry(normalization.normalization_id)
    assert excinfo.value.code == "VALIDATION_NOT_FAILED"

    await svc.start(normalization.normalization_id)  # -> COMPLETED
    with pytest.raises(ConflictError) as excinfo:
        await svc.retry(normalization.normalization_id)
    assert excinfo.value.code == "VALIDATION_NOT_FAILED"


async def test_two_concurrent_starts_only_one_wins(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup:
        normalization_id = (
            await _completed_normalization(setup)
        ).normalization_id

    async def run() -> ValidationAttempt:
        async with session_factory() as session:
            return await ValidationService(session).start(normalization_id)

    results = await asyncio.gather(run(), run(), return_exceptions=True)

    winners = [r for r in results if isinstance(r, ValidationAttempt)]
    conflicts = [r for r in results if isinstance(r, ConflictError)]
    assert len(winners) == 1 and len(conflicts) == 1
    assert winners[0].status is ValidationStatus.COMPLETED
    assert conflicts[0].code in {
        "VALIDATION_IN_PROGRESS",
        "NORMALIZATION_ALREADY_VALIDATED",
    }

    async with session_factory() as check:
        history = await ValidationRepository(check).list_for_normalization(
            normalization_id
        )
    assert [a.attempt_number for a in history] == [1]


# --- technical failure handling -----------------------------------------


async def test_engine_exception_marks_failed_with_a_safe_message(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    normalization = await _completed_normalization(db_session)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("engine blew up host=10.0.0.9 key=sk-secret")

    monkeypatch.setattr(_ENGINE_PATH, boom)

    attempt = await ValidationService(db_session).start(
        normalization.normalization_id
    )

    assert attempt.status is ValidationStatus.FAILED
    assert attempt.failure_code == "VALIDATION_FAILED"
    assert attempt.failure_message == _GENERIC_FAILURE
    assert "sk-secret" not in (attempt.failure_message or "")
    assert attempt.completed_at is not None
    assert list(attempt.findings) == []


async def test_persist_failure_rolls_back_and_stores_no_partial_result(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    normalization = await _completed_normalization(db_session)

    def explode(self, attempt, result) -> None:  # noqa: ANN001
        # Half-write a finding, then fail - the service must roll it back.
        attempt.findings = [
            ValidationFindingRow(
                position=0,
                rule=ValidationRule.NO_LINE_ITEMS,
                severity="info",
                field_path=None,
                expected=None,
                actual=None,
                message="x",
                context={},
            )
        ]
        raise RuntimeError("database write failed mid-flush")

    monkeypatch.setattr(ValidationRepository, "apply_result", explode)

    attempt = await ValidationService(db_session).start(
        normalization.normalization_id
    )

    assert attempt.status is ValidationStatus.FAILED
    assert attempt.failure_code == "VALIDATION_FAILED"
    assert list(attempt.findings) == []

    db_session.expire_all()
    finding_count = await db_session.scalar(
        select(func.count()).select_from(ValidationFindingRow)
    )
    assert finding_count == 0


async def test_retry_after_technical_failure_creates_new_attempt_and_completes(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    normalization = await _completed_normalization(db_session)
    svc = ValidationService(db_session)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("transient engine crash")

    monkeypatch.setattr(_ENGINE_PATH, boom)
    first = await svc.start(normalization.normalization_id)
    assert first.status is ValidationStatus.FAILED

    monkeypatch.undo()
    second = await svc.retry(normalization.normalization_id)
    assert second.status is ValidationStatus.COMPLETED
    assert second.attempt_number == 2

    history = await ValidationRepository(db_session).list_for_normalization(
        normalization.normalization_id
    )
    assert [(a.attempt_number, a.status) for a in history] == [
        (1, ValidationStatus.FAILED),
        (2, ValidationStatus.COMPLETED),
    ]
    # The failed attempt was not mutated by the retry.
    assert history[0].failure_code == "VALIDATION_FAILED"
    assert list(history[0].findings) == []
    assert len(history[1].findings) > 0


# --- source immutability -------------------------------------------------


async def test_source_normalization_and_document_are_untouched(
    db_session: AsyncSession,
) -> None:
    normalization = await _completed_normalization(db_session)
    normalization_id = normalization.normalization_id
    extraction_id = normalization.extraction_id
    document_id = (
        await db_session.execute(
            select(ExtractionAttempt.document_id).where(
                ExtractionAttempt.extraction_id == extraction_id
            )
        )
    ).scalar_one()

    norm_updated_at = normalization.updated_at
    doc_fingerprint = (
        await db_session.execute(
            select(
                Document.file_location, Document.file_hash, Document.file_size_bytes
            ).where(Document.document_id == document_id)
        )
    ).one()

    await ValidationService(db_session).start(normalization_id)

    db_session.expire_all()
    reloaded_norm = await db_session.get(NormalizationAttempt, normalization_id)
    assert reloaded_norm.status is NormalizationStatus.COMPLETED
    assert reloaded_norm.updated_at == norm_updated_at
    assert reloaded_norm.total_amount == Decimal("130.00")

    reloaded_doc_fingerprint = (
        await db_session.execute(
            select(
                Document.file_location, Document.file_hash, Document.file_size_bytes
            ).where(Document.document_id == document_id)
        )
    ).one()
    assert reloaded_doc_fingerprint == doc_fingerprint


# --- lifecycle guards (re-exercised through the service) ----------------


def test_lifecycle_attempt_transition_table() -> None:
    lifecycle.ensure_attempt_transition(
        ValidationStatus.PROCESSING, ValidationStatus.COMPLETED
    )
    lifecycle.ensure_attempt_transition(
        ValidationStatus.PROCESSING, ValidationStatus.FAILED
    )
    for bad in (
        (ValidationStatus.PROCESSING, ValidationStatus.PROCESSING),
        (ValidationStatus.COMPLETED, ValidationStatus.FAILED),
        (ValidationStatus.FAILED, ValidationStatus.COMPLETED),
        (ValidationStatus.COMPLETED, ValidationStatus.PROCESSING),
    ):
        with pytest.raises(ValueError):
            lifecycle.ensure_attempt_transition(*bad)
