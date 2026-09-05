"""Tests for ``DecisionRepository`` (Stage 6, package 2).

There is no decision service yet (that is package 4), so unlike Stage 5's
repository - exercised only indirectly through
``ValidationService`` - this repository is tested directly against a real
session.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DecisionOutcome,
    DecisionStatus,
    Document,
    ExtractionAttempt,
    ExtractionStatus,
    NormalizationAttempt,
    NormalizationStatus,
    ValidationAttempt,
    ValidationStatus,
)
from app.schemas.decision import DecisionReason, InvoiceDecision
from app.services.processing.decision.repository import DecisionRepository

_POLICY_VERSION = "1"


async def _make_validation(session: AsyncSession) -> ValidationAttempt:
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
    return validation


def _clean_decision() -> InvoiceDecision:
    return InvoiceDecision.from_reasons([])


def _review_decision() -> InvoiceDecision:
    return InvoiceDecision.from_reasons(
        [
            DecisionReason.model_validate(
                {
                    "code": "manual_review_requested",
                    "triggers_review": True,
                    "source_rule": None,
                    "field_path": None,
                    "message": "A manual review of this invoice was requested.",
                }
            )
        ]
    )


# --- next_attempt_number / add_attempt ----------------------------------


async def test_next_attempt_number_starts_at_one(db_session: AsyncSession) -> None:
    validation = await _make_validation(db_session)
    repo = DecisionRepository(db_session)
    assert await repo.next_attempt_number(validation.validation_id) == 1


async def test_add_attempt_creates_a_processing_row_with_policy_version(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    repo = DecisionRepository(db_session)
    attempt = repo.add_attempt(
        validation_id=validation.validation_id,
        attempt_number=1,
        policy_version=_POLICY_VERSION,
    )
    await db_session.commit()

    fetched = await repo.get(attempt.decision_id)
    assert fetched is not None
    assert fetched.status is DecisionStatus.PROCESSING
    assert fetched.policy_version == _POLICY_VERSION
    assert fetched.outcome is None
    assert fetched.reasons == []


async def test_next_attempt_number_increments_after_a_completed_attempt(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    repo = DecisionRepository(db_session)
    attempt = repo.add_attempt(
        validation_id=validation.validation_id,
        attempt_number=1,
        policy_version=_POLICY_VERSION,
    )
    attempt.status = DecisionStatus.FAILED
    attempt.completed_at = datetime.now(timezone.utc)
    attempt.failure_code = "DECISION_FAILED"
    attempt.failure_message = "Decision failed."
    await db_session.commit()

    assert await repo.next_attempt_number(validation.validation_id) == 2


# --- apply_result --------------------------------------------------------


async def test_apply_result_stores_outcome_and_reasons(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    repo = DecisionRepository(db_session)
    attempt = repo.add_attempt(
        validation_id=validation.validation_id,
        attempt_number=1,
        policy_version=_POLICY_VERSION,
    )
    await db_session.flush()

    # Re-fetch through the eager-loading get() before mutating .reasons -
    # assigning a relationship collection on an already-persistent object
    # whose collection was never loaded triggers an implicit lazy load,
    # which fails outside a greenlet under the async engine. The package 4
    # service will follow this same fetch-then-apply_result pattern (see
    # ValidationService._complete).
    attempt = await repo.get(attempt.decision_id)
    result = _review_decision()
    repo.apply_result(attempt, result, [None])
    attempt.status = DecisionStatus.COMPLETED
    attempt.completed_at = datetime.now(timezone.utc)
    await db_session.commit()

    fetched = await repo.get(attempt.decision_id)
    assert fetched.outcome is DecisionOutcome.NEEDS_REVIEW
    assert len(fetched.reasons) == 1
    assert fetched.reasons[0].code.value == "manual_review_requested"
    assert fetched.reasons[0].source_finding_id is None


async def test_apply_result_with_no_reasons_is_accepted_and_empty(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    repo = DecisionRepository(db_session)
    attempt = repo.add_attempt(
        validation_id=validation.validation_id,
        attempt_number=1,
        policy_version=_POLICY_VERSION,
    )
    await db_session.flush()

    attempt = await repo.get(attempt.decision_id)
    repo.apply_result(attempt, _clean_decision(), [])
    attempt.status = DecisionStatus.COMPLETED
    attempt.completed_at = datetime.now(timezone.utc)
    await db_session.commit()

    fetched = await repo.get(attempt.decision_id)
    assert fetched.outcome is DecisionOutcome.ACCEPTED
    assert fetched.reasons == []


# --- reads: get_for_validation / list / latest / active -----------------


async def test_get_for_validation_scopes_to_the_owning_validation(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    other_validation = await _make_validation(db_session)
    repo = DecisionRepository(db_session)
    attempt = repo.add_attempt(
        validation_id=validation.validation_id,
        attempt_number=1,
        policy_version=_POLICY_VERSION,
    )
    await db_session.commit()

    assert await repo.get_for_validation(validation.validation_id, attempt.decision_id) is not None
    assert (
        await repo.get_for_validation(other_validation.validation_id, attempt.decision_id)
        is None
    )


async def test_list_for_validation_is_oldest_first(db_session: AsyncSession) -> None:
    validation = await _make_validation(db_session)
    repo = DecisionRepository(db_session)
    first = repo.add_attempt(
        validation_id=validation.validation_id,
        attempt_number=1,
        policy_version=_POLICY_VERSION,
    )
    first.status = DecisionStatus.FAILED
    first.completed_at = datetime.now(timezone.utc)
    first.failure_code = "DECISION_FAILED"
    first.failure_message = "Decision failed."
    await db_session.flush()
    second = repo.add_attempt(
        validation_id=validation.validation_id,
        attempt_number=2,
        policy_version=_POLICY_VERSION,
    )
    await db_session.commit()

    attempts = await repo.list_for_validation(validation.validation_id)
    assert [a.decision_id for a in attempts] == [first.decision_id, second.decision_id]


async def test_latest_for_validation_returns_the_highest_attempt_number(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    repo = DecisionRepository(db_session)
    first = repo.add_attempt(
        validation_id=validation.validation_id,
        attempt_number=1,
        policy_version=_POLICY_VERSION,
    )
    first.status = DecisionStatus.FAILED
    first.completed_at = datetime.now(timezone.utc)
    first.failure_code = "DECISION_FAILED"
    first.failure_message = "Decision failed."
    await db_session.flush()
    second = repo.add_attempt(
        validation_id=validation.validation_id,
        attempt_number=2,
        policy_version=_POLICY_VERSION,
    )
    await db_session.commit()

    latest = await repo.latest_for_validation(validation.validation_id)
    assert latest.decision_id == second.decision_id


async def test_active_for_validation_finds_the_processing_attempt(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    repo = DecisionRepository(db_session)
    attempt = repo.add_attempt(
        validation_id=validation.validation_id,
        attempt_number=1,
        policy_version=_POLICY_VERSION,
    )
    await db_session.commit()

    active = await repo.active_for_validation(validation.validation_id)
    assert active is not None
    assert active.decision_id == attempt.decision_id


async def test_active_for_validation_is_none_once_completed(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    repo = DecisionRepository(db_session)
    attempt = repo.add_attempt(
        validation_id=validation.validation_id,
        attempt_number=1,
        policy_version=_POLICY_VERSION,
    )
    await db_session.flush()
    attempt = await repo.get(attempt.decision_id)
    repo.apply_result(attempt, _clean_decision(), [])
    attempt.status = DecisionStatus.COMPLETED
    attempt.completed_at = datetime.now(timezone.utc)
    await db_session.commit()

    assert await repo.active_for_validation(validation.validation_id) is None
