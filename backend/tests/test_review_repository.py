"""Tests for ``ReviewRepository`` (Stage 7, packages 1 and 2).

The persistence reads/writes and the package 2 queue query are tested directly
against a real session, mirroring ``tests/test_decision_repository.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DecisionAttempt,
    DecisionOutcome,
    DecisionStatus,
    Document,
    DocumentStatus,
    ExtractionAttempt,
    ExtractionStatus,
    NormalizationAttempt,
    NormalizationStatus,
    ReviewAction,
    ValidationAttempt,
    ValidationStatus,
)
from app.schemas.review import REVIEW_POLICY_VERSION, InvoiceReview
from app.services.processing.review.repository import ReviewRepository

_POLICY_VERSION = "test"


async def _make_decision(session: AsyncSession) -> DecisionAttempt:
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
    decision = DecisionAttempt(
        validation_id=validation.validation_id,
        attempt_number=1,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.NEEDS_REVIEW,
        policy_version=_POLICY_VERSION,
        completed_at=datetime.now(timezone.utc),
    )
    session.add(decision)
    await session.flush()
    return decision


def _approve() -> InvoiceReview:
    return InvoiceReview.model_validate(
        {"action": "APPROVE", "reviewer_name": "Dana Ops", "note": None}
    )


def _reject() -> InvoiceReview:
    return InvoiceReview.model_validate(
        {"action": "REJECT", "reviewer_name": "Dana Ops", "note": "duplicate"}
    )


async def test_add_stamps_policy_version_and_timestamps(
    db_session: AsyncSession,
) -> None:
    decision = await _make_decision(db_session)
    repo = ReviewRepository(db_session)

    when = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    record = repo.add(
        decision_id=decision.decision_id,
        review=_reject(),
        policy_version=REVIEW_POLICY_VERSION,
        reviewed_at=when,
    )
    await db_session.flush()

    assert record.action is ReviewAction.REJECT
    assert record.note == "duplicate"
    assert record.policy_version == REVIEW_POLICY_VERSION
    assert record.reviewed_at == when
    assert record.created_at == when


async def test_reads_find_the_review_by_id_and_by_decision(
    db_session: AsyncSession,
) -> None:
    decision = await _make_decision(db_session)
    other = await _make_decision(db_session)
    repo = ReviewRepository(db_session)

    assert await repo.exists_for_decision(decision.decision_id) is False
    assert await repo.get_by_decision(decision.decision_id) is None

    record = repo.add(
        decision_id=decision.decision_id,
        review=_approve(),
        policy_version=REVIEW_POLICY_VERSION,
    )
    await db_session.flush()

    assert await repo.exists_for_decision(decision.decision_id) is True
    assert (await repo.get(record.review_id)).review_id == record.review_id
    assert (
        await repo.get_by_decision(decision.decision_id)
    ).review_id == record.review_id
    assert (
        await repo.get_for_decision(decision.decision_id, record.review_id)
    ).review_id == record.review_id
    # not cross-wired to the other decision
    assert await repo.get_for_decision(other.decision_id, record.review_id) is None
    assert await repo.get(uuid.uuid4()) is None


async def test_a_second_review_for_one_decision_fails_on_flush(
    db_session: AsyncSession,
) -> None:
    decision = await _make_decision(db_session)
    repo = ReviewRepository(db_session)

    repo.add(
        decision_id=decision.decision_id,
        review=_approve(),
        policy_version=REVIEW_POLICY_VERSION,
    )
    await db_session.flush()

    repo.add(
        decision_id=decision.decision_id,
        review=_reject(),
        policy_version=REVIEW_POLICY_VERSION,
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_queue_excludes_a_decision_from_a_superseded_chain(
    db_session: AsyncSession,
) -> None:
    decision = await _make_decision(db_session)
    repo = ReviewRepository(db_session)

    assert [row[1].decision_id for row in await repo.queue()] == [
        decision.decision_id
    ]

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

    assert await repo.queue() == []
