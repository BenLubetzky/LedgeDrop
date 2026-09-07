"""Tests for the Stage 7 review persistence model (package 1)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
    ReviewAction,
    ReviewRecord,
    ValidationAttempt,
    ValidationStatus,
)

_POLICY_VERSION = "test"


async def _make_decision(
    session: AsyncSession, *, outcome: DecisionOutcome = DecisionOutcome.NEEDS_REVIEW
) -> DecisionAttempt:
    """A COMPLETED decision attempt with its whole document -> validation chain."""
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
        outcome=outcome,
        policy_version=_POLICY_VERSION,
        completed_at=datetime.now(timezone.utc),
    )
    session.add(decision)
    await session.flush()
    return decision


def _record(decision_id: uuid.UUID, **overrides) -> ReviewRecord:
    kwargs = {
        "decision_id": decision_id,
        "action": ReviewAction.APPROVE,
        "reviewer_name": "Dana Ops",
        "note": None,
        "policy_version": _POLICY_VERSION,
    }
    kwargs.update(overrides)
    return ReviewRecord(**kwargs)


async def test_create_review_and_relationship(db_session: AsyncSession) -> None:
    decision = await _make_decision(db_session)
    db_session.add(
        _record(decision.decision_id, action=ReviewAction.REJECT, note="wrong vendor")
    )
    await db_session.commit()

    loaded = (
        await db_session.execute(
            select(DecisionAttempt).where(
                DecisionAttempt.decision_id == decision.decision_id
            )
        )
    ).scalar_one()
    await db_session.refresh(loaded, ["review"])
    assert loaded.review is not None
    assert loaded.review.action is ReviewAction.REJECT
    assert loaded.review.note == "wrong vendor"
    assert loaded.review.source_decision is loaded


async def test_one_review_per_decision(db_session: AsyncSession) -> None:
    decision = await _make_decision(db_session)
    db_session.add(_record(decision.decision_id))
    await db_session.flush()
    db_session.add(_record(decision.decision_id, reviewer_name="Someone Else"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_blank_reviewer_name_is_rejected_by_the_database(
    db_session: AsyncSession,
) -> None:
    decision = await _make_decision(db_session)
    db_session.add(_record(decision.decision_id, reviewer_name="   "))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_reject_without_a_note_is_rejected_by_the_database(
    db_session: AsyncSession,
) -> None:
    decision = await _make_decision(db_session)
    db_session.add(
        _record(decision.decision_id, action=ReviewAction.REJECT, note=None)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize("action", [ReviewAction.APPROVE, ReviewAction.REJECT])
async def test_a_blank_note_is_rejected_by_the_database(
    db_session: AsyncSession, action: ReviewAction
) -> None:
    decision = await _make_decision(db_session)
    db_session.add(_record(decision.decision_id, action=action, note="   "))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_approve_with_no_note_is_accepted(db_session: AsyncSession) -> None:
    decision = await _make_decision(db_session)
    db_session.add(_record(decision.decision_id, action=ReviewAction.APPROVE, note=None))
    await db_session.flush()


async def test_document_status_accepts_the_new_terminal_labels(
    db_session: AsyncSession,
) -> None:
    labels = set(
        (
            await db_session.execute(
                sa.text(
                    "SELECT unnest(enum_range(NULL::document_status))::text"
                )
            )
        ).scalars()
    )
    assert {"APPROVED", "REJECTED"} <= labels

    decision = await _make_decision(db_session)
    doc = (
        await db_session.execute(
            select(Document).join(
                ExtractionAttempt, ExtractionAttempt.document_id == Document.document_id
            )
        )
    ).scalars().first()
    doc.status = DocumentStatus.APPROVED
    await db_session.flush()
    assert doc.status is DocumentStatus.APPROVED


async def test_deleting_document_cascades_to_the_review(
    db_session: AsyncSession,
) -> None:
    decision = await _make_decision(db_session)
    db_session.add(_record(decision.decision_id))
    await db_session.commit()

    doc_id = (
        await db_session.execute(select(ExtractionAttempt.document_id))
    ).scalar_one()
    doc = await db_session.get(Document, doc_id)
    await db_session.delete(doc)
    await db_session.commit()

    remaining = (await db_session.execute(select(ReviewRecord))).scalars().all()
    assert remaining == []
