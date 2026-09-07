"""Tests for ``ReviewService.submit`` (Stage 7, package 2).

Exercised directly against a real session (there is a thin API layer on top,
covered by ``tests/test_reviews_api.py``). Mirrors
``tests/test_decision_service.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError, NotFoundError
from app.models import (
    DecisionAttempt,
    DecisionOutcome,
    DecisionReasonRow,
    DecisionStatus,
    Document,
    DocumentStatus,
    ExtractionAttempt,
    ExtractionStatus,
    NormalizationAttempt,
    NormalizationStatus,
    ReviewRecord,
    ValidationAttempt,
    ValidationStatus,
)
from app.schemas.review import InvoiceReview
from app.services.processing.review import ReviewService
from app.services.processing.review.repository import ReviewRepository

_DPV = "test"


async def _chain(session: AsyncSession) -> tuple[Document, ExtractionAttempt, NormalizationAttempt, ValidationAttempt]:
    doc = Document(
        original_filename="invoice.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="a" * 64,
        file_size_bytes=1234,
        page_count=1,
        status=DocumentStatus.NEEDS_REVIEW,
    )
    session.add(doc)
    await session.flush()
    extraction = ExtractionAttempt(
        document_id=doc.document_id,
        attempt_number=1,
        status=ExtractionStatus.COMPLETED,
        completed_at=datetime.now(timezone.utc),
        provider_name="fake",
    )
    session.add(extraction)
    await session.flush()
    normalization = NormalizationAttempt(
        extraction_id=extraction.extraction_id,
        attempt_number=1,
        status=NormalizationStatus.COMPLETED,
        completed_at=datetime.now(timezone.utc),
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
    return doc, extraction, normalization, validation


async def _decision(
    session: AsyncSession,
    *,
    status: DecisionStatus = DecisionStatus.COMPLETED,
    outcome: DecisionOutcome | None = DecisionOutcome.NEEDS_REVIEW,
    doc_status: DocumentStatus = DocumentStatus.NEEDS_REVIEW,
) -> DecisionAttempt:
    doc, _extraction, _normalization, validation = await _chain(session)
    doc.status = doc_status
    failed = status is DecisionStatus.FAILED
    decision = DecisionAttempt(
        validation_id=validation.validation_id,
        attempt_number=1,
        status=status,
        outcome=outcome if status is DecisionStatus.COMPLETED else None,
        policy_version=_DPV,
        completed_at=(
            datetime.now(timezone.utc)
            if status is not DecisionStatus.PROCESSING
            else None
        ),
        failure_code="DECISION_FAILED" if failed else None,
        failure_message="Decision did not complete." if failed else None,
    )
    if status is DecisionStatus.COMPLETED and outcome is DecisionOutcome.NEEDS_REVIEW:
        decision.reasons = [
            DecisionReasonRow(
                position=0,
                code="manual_review_requested",
                triggers_review=True,
                source_rule=None,
                source_finding_id=None,
                field_path=None,
                message="A manual review of this invoice was requested.",
            )
        ]
    session.add(decision)
    await session.flush()
    return decision


def _approve() -> InvoiceReview:
    return InvoiceReview.model_validate(
        {"action": "APPROVE", "reviewer_name": "Dana Ops", "note": None}
    )


def _reject() -> InvoiceReview:
    return InvoiceReview.model_validate(
        {"action": "REJECT", "reviewer_name": "Dana Ops", "note": "wrong vendor"}
    )


async def _doc_status(
    session: AsyncSession, validation_id: uuid.UUID
) -> DocumentStatus:
    return (
        await session.execute(
            sa.select(Document.status)
            .join(ExtractionAttempt, ExtractionAttempt.document_id == Document.document_id)
            .join(
                NormalizationAttempt,
                NormalizationAttempt.extraction_id == ExtractionAttempt.extraction_id,
            )
            .join(
                ValidationAttempt,
                ValidationAttempt.normalization_id
                == NormalizationAttempt.normalization_id,
            )
            .where(ValidationAttempt.validation_id == validation_id)
        )
    ).scalar_one()


# --- happy paths ---------------------------------------------------


async def test_approve_records_the_review_and_moves_the_document(
    db_session: AsyncSession,
) -> None:
    decision = await _decision(db_session)
    vid = decision.validation_id
    record = await ReviewService(db_session).submit(decision.decision_id, _approve())

    assert record.action.value == "APPROVE"
    assert record.policy_version  # stamped
    assert await _doc_status(db_session, vid) is DocumentStatus.APPROVED


async def test_reject_records_the_note_and_moves_the_document(
    db_session: AsyncSession,
) -> None:
    decision = await _decision(db_session)
    vid = decision.validation_id
    record = await ReviewService(db_session).submit(decision.decision_id, _reject())

    assert record.action.value == "REJECT"
    assert record.note == "wrong vendor"
    assert await _doc_status(db_session, vid) is DocumentStatus.REJECTED


# --- guards ------------------------------------------------------


async def test_unknown_decision_is_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError) as err:
        await ReviewService(db_session).submit(uuid.uuid4(), _approve())
    assert err.value.code == "DECISION_NOT_FOUND"


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (DecisionStatus.PROCESSING, None),
        (DecisionStatus.FAILED, None),
        (DecisionStatus.COMPLETED, DecisionOutcome.ACCEPTED),
    ],
)
async def test_non_reviewable_decision_is_rejected_and_writes_nothing(
    db_session: AsyncSession, status, outcome
) -> None:
    doc_status = (
        DocumentStatus.COMPLETED
        if outcome is DecisionOutcome.ACCEPTED
        else DocumentStatus.PROCESSING
        if status is DecisionStatus.PROCESSING
        else DocumentStatus.COMPLETED
    )
    decision = await _decision(
        db_session, status=status, outcome=outcome, doc_status=doc_status
    )
    with pytest.raises(ConflictError) as err:
        await ReviewService(db_session).submit(decision.decision_id, _approve())
    assert err.value.code == "DECISION_NOT_REVIEWABLE"

    await db_session.rollback()
    assert (await db_session.execute(sa.select(ReviewRecord))).first() is None


async def test_second_submit_conflicts_and_keeps_the_first_outcome(
    db_session: AsyncSession,
) -> None:
    decision = await _decision(db_session)
    vid = decision.validation_id
    await ReviewService(db_session).submit(decision.decision_id, _approve())

    with pytest.raises(ConflictError) as err:
        await ReviewService(db_session).submit(decision.decision_id, _reject())
    assert err.value.code == "DECISION_ALREADY_REVIEWED"

    await db_session.rollback()
    rows = (await db_session.execute(sa.select(ReviewRecord))).scalars().all()
    assert len(rows) == 1 and rows[0].action.value == "APPROVE"
    assert await _doc_status(db_session, vid) is DocumentStatus.APPROVED


async def test_stale_decision_source_is_rejected(db_session: AsyncSession) -> None:
    decision = await _decision(db_session)
    # A newer extraction attempt for the same document supersedes the chain the
    # decision came from.
    document_id = (
        await db_session.execute(
            sa.select(ExtractionAttempt.document_id)
            .join(
                NormalizationAttempt,
                NormalizationAttempt.extraction_id == ExtractionAttempt.extraction_id,
            )
            .join(
                ValidationAttempt,
                ValidationAttempt.normalization_id
                == NormalizationAttempt.normalization_id,
            )
            .where(ValidationAttempt.validation_id == decision.validation_id)
        )
    ).scalar_one()
    db_session.add(
        ExtractionAttempt(
            document_id=document_id,
            attempt_number=2,
            status=ExtractionStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            provider_name="fake",
        )
    )
    await db_session.flush()

    with pytest.raises(ConflictError) as err:
        await ReviewService(db_session).submit(decision.decision_id, _approve())
    assert err.value.code == "STALE_DECISION_SOURCE"


async def test_source_rows_are_untouched_apart_from_document_status(
    db_session: AsyncSession,
) -> None:
    decision = await _decision(db_session)
    vid = decision.validation_id
    await db_session.commit()

    before = {
        t: [dict(r) for r in (await db_session.execute(sa.text(f'SELECT * FROM "{t}" ORDER BY 1'))).mappings()]
        for t in ("invoice_extractions", "invoice_normalizations", "invoice_validations", "invoice_decisions", "invoice_decision_reasons")
    }

    await ReviewService(db_session).submit(decision.decision_id, _reject())
    await db_session.rollback()

    after = {
        t: [dict(r) for r in (await db_session.execute(sa.text(f'SELECT * FROM "{t}" ORDER BY 1'))).mappings()]
        for t in before
    }
    assert after == before
    assert await _doc_status(db_session, vid) is DocumentStatus.REJECTED


# --- concurrency -----------------------------------------------


async def test_two_concurrent_submits_only_one_wins(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup:
        decision = await _decision(setup)
        await setup.commit()
        decision_id = decision.decision_id

    async def run(review: InvoiceReview) -> ReviewRecord:
        async with session_factory() as session:
            return await ReviewService(session).submit(decision_id, review)

    results = await asyncio.gather(
        run(_approve()), run(_reject()), return_exceptions=True
    )
    winners = [r for r in results if isinstance(r, ReviewRecord)]
    conflicts = [r for r in results if isinstance(r, ConflictError)]
    assert len(winners) == 1 and len(conflicts) == 1
    assert conflicts[0].code == "DECISION_ALREADY_REVIEWED"

    async with session_factory() as check:
        rows = (await check.execute(sa.select(ReviewRecord))).scalars().all()
        assert len(rows) == 1
        status = (
            await check.execute(
                sa.select(Document.status).where(
                    Document.status.in_(
                        [DocumentStatus.APPROVED, DocumentStatus.REJECTED]
                    )
                )
            )
        ).scalar_one()
        assert status in (DocumentStatus.APPROVED, DocumentStatus.REJECTED)
