"""Tests for the Stage 6 decision persistence models (package 2)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DecisionAttempt,
    DecisionOutcome,
    DecisionReasonCode,
    DecisionReasonRow,
    DecisionStatus,
    Document,
    ExtractionAttempt,
    ExtractionStatus,
    FindingSeverity,
    NormalizationAttempt,
    NormalizationStatus,
    ValidationAttempt,
    ValidationFindingRow,
    ValidationRule,
    ValidationStatus,
)

_POLICY_VERSION = "1"


async def _make_validation(session: AsyncSession) -> ValidationAttempt:
    """A COMPLETED validation attempt with its document/extraction/normalization chain."""
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


async def _attach_finding(
    session: AsyncSession, validation: ValidationAttempt, finding: ValidationFindingRow
) -> None:
    """Persist ``finding`` under an already-flushed ``validation``.

    Assigning to ``validation.findings`` directly would trigger a lazy load
    of the relationship's current value (``validation`` is already
    persistent), which fails outside a greenlet under the async engine.
    Setting the foreign key and adding the row directly avoids touching the
    collection relationship at all.
    """
    finding.validation_id = validation.validation_id
    session.add(finding)
    await session.flush()


def _validation_finding(position: int = 0, **kw) -> ValidationFindingRow:
    data = {
        "position": position,
        "rule": ValidationRule.TOTALS_DO_NOT_RECONCILE,
        "severity": FindingSeverity.WARNING,
        "field_path": None,
        "expected": "119.00",
        "actual": "120.00",
        "message": "The subtotal plus tax does not equal the invoice total.",
        "context": {"delta": "-1.00"},
    }
    data.update(kw)
    return ValidationFindingRow(**data)


def _decision(validation_id: uuid.UUID, *, number: int = 1, **kw) -> DecisionAttempt:
    status = kw.pop("status", DecisionStatus.PROCESSING)
    if status is DecisionStatus.COMPLETED:
        kw.setdefault("completed_at", datetime.now(timezone.utc))
        kw.setdefault("outcome", DecisionOutcome.ACCEPTED)
    elif status is DecisionStatus.FAILED:
        kw.setdefault("completed_at", datetime.now(timezone.utc))
        kw.setdefault("failure_code", "DECISION_FAILED")
        kw.setdefault("failure_message", "Decision failed.")
    return DecisionAttempt(
        validation_id=validation_id,
        attempt_number=number,
        status=status,
        policy_version=kw.pop("policy_version", _POLICY_VERSION),
        **kw,
    )


def _reason(position: int, *, finding_id: uuid.UUID | None, **kw) -> DecisionReasonRow:
    data = {
        "position": position,
        "code": DecisionReasonCode.TOTALS_DO_NOT_RECONCILE,
        "triggers_review": True,
        "source_rule": "totals_do_not_reconcile",
        "source_finding_id": finding_id,
        "field_path": None,
        "message": "The subtotal plus tax does not equal the invoice total.",
    }
    data.update(kw)
    return DecisionReasonRow(**data)


def _manual_reason(position: int, **kw) -> DecisionReasonRow:
    data = {
        "position": position,
        "code": DecisionReasonCode.MANUAL_REVIEW_REQUESTED,
        "triggers_review": True,
        "source_rule": None,
        "source_finding_id": None,
        "field_path": None,
        "message": "A manual review of this invoice was requested.",
    }
    data.update(kw)
    return DecisionReasonRow(**data)


# --- happy path: attempt + reasons + relationships ---------------------


async def test_create_attempt_with_reasons_and_relationships(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    finding = _validation_finding(0)
    await _attach_finding(db_session, validation, finding)

    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.NEEDS_REVIEW,
    )
    attempt.reasons = [
        _reason(0, finding_id=finding.validation_finding_id),
        _manual_reason(1),
    ]
    db_session.add(attempt)
    await db_session.commit()

    fetched = (
        await db_session.execute(
            select(DecisionAttempt).where(
                DecisionAttempt.validation_id == validation.validation_id
            )
        )
    ).scalar_one()
    assert fetched.attempt_number == 1
    assert fetched.status is DecisionStatus.COMPLETED
    assert fetched.outcome is DecisionOutcome.NEEDS_REVIEW
    assert fetched.policy_version == _POLICY_VERSION
    assert fetched.completed_at is not None
    assert fetched.failure_code is None
    assert [r.position for r in fetched.reasons] == [0, 1]
    assert fetched.reasons[0].code is DecisionReasonCode.TOTALS_DO_NOT_RECONCILE
    assert fetched.reasons[0].source_finding_id == finding.validation_finding_id
    assert fetched.reasons[1].code is DecisionReasonCode.MANUAL_REVIEW_REQUESTED
    assert fetched.reasons[1].source_rule is None
    assert fetched.reasons[1].source_finding_id is None

    await db_session.refresh(validation, ["decisions"])
    assert [d.decision_id for d in validation.decisions] == [fetched.decision_id]
    assert fetched.source_validation.validation_id == validation.validation_id


# --- attempt lifecycle / uniqueness, mirroring Stage 5 ------------------


async def test_multiple_attempts_per_validation_are_allowed(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    db_session.add_all(
        [
            _decision(validation.validation_id, number=1, status=DecisionStatus.FAILED),
            _decision(validation.validation_id, number=2, status=DecisionStatus.COMPLETED),
        ]
    )
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(DecisionAttempt.attempt_number)
                .where(DecisionAttempt.validation_id == validation.validation_id)
                .order_by(DecisionAttempt.attempt_number)
            )
        )
        .scalars()
        .all()
    )
    assert rows == [1, 2]


async def test_attempt_number_is_unique_per_validation(db_session: AsyncSession) -> None:
    validation = await _make_validation(db_session)
    db_session.add(
        _decision(validation.validation_id, number=1, status=DecisionStatus.FAILED)
    )
    await db_session.commit()

    db_session.add(_decision(validation.validation_id, number=1))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_only_one_active_attempt_per_validation(db_session: AsyncSession) -> None:
    validation = await _make_validation(db_session)
    db_session.add(
        _decision(validation.validation_id, number=1, status=DecisionStatus.PROCESSING)
    )
    await db_session.commit()

    db_session.add(
        _decision(validation.validation_id, number=2, status=DecisionStatus.PROCESSING)
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_second_non_active_attempt_is_fine(db_session: AsyncSession) -> None:
    validation = await _make_validation(db_session)
    db_session.add(
        _decision(validation.validation_id, number=1, status=DecisionStatus.PROCESSING)
    )
    await db_session.commit()
    db_session.add(
        _decision(validation.validation_id, number=2, status=DecisionStatus.FAILED)
    )
    await db_session.commit()  # no error


async def test_attempt_number_must_be_positive(db_session: AsyncSession) -> None:
    validation = await _make_validation(db_session)
    db_session.add(
        _decision(validation.validation_id, number=0, status=DecisionStatus.COMPLETED)
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


# --- status / outcome consistency --------------------------------------


@pytest.mark.parametrize(
    ("status", "fields"),
    [
        (DecisionStatus.PROCESSING, {"completed_at": datetime.now(timezone.utc)}),
        (DecisionStatus.PROCESSING, {"failure_code": "EARLY"}),
        (DecisionStatus.PROCESSING, {"outcome": DecisionOutcome.ACCEPTED}),
        (DecisionStatus.COMPLETED, {"completed_at": None}),
        (DecisionStatus.COMPLETED, {"failure_message": "should not be here"}),
        (DecisionStatus.COMPLETED, {"outcome": None}),
        (DecisionStatus.FAILED, {"completed_at": None}),
        (DecisionStatus.FAILED, {"failure_code": None}),
        (DecisionStatus.FAILED, {"failure_message": None}),
        (DecisionStatus.FAILED, {"outcome": DecisionOutcome.ACCEPTED}),
    ],
)
async def test_status_dependent_fields_are_enforced(
    db_session: AsyncSession,
    status: DecisionStatus,
    fields: dict,
) -> None:
    validation = await _make_validation(db_session)
    db_session.add(_decision(validation.validation_id, status=status, **fields))
    with pytest.raises(IntegrityError):
        await db_session.commit()


# --- reason position ---------------------------------------------------


async def test_reason_position_must_be_unique_and_non_negative(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.ACCEPTED,
    )
    attempt.reasons = [_manual_reason(0), _manual_reason(0)]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_reason_negative_position_is_rejected(db_session: AsyncSession) -> None:
    validation = await _make_validation(db_session)
    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.ACCEPTED,
    )
    attempt.reasons = [_manual_reason(-1)]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


# --- reason field_path shape, reused from Stage 4/5 ---------------------


@pytest.mark.parametrize(
    "field_path",
    [
        None,
        "invoice_number",
        "total_amount",
        "currency",
        "line_items.0.line_total",
        "line_items.12.unit_price",
    ],
)
async def test_reason_field_path_accepts_null_and_stage4_paths(
    db_session: AsyncSession, field_path: str | None
) -> None:
    validation = await _make_validation(db_session)
    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.ACCEPTED,
    )
    attempt.reasons = [_manual_reason(0, field_path=field_path)]
    db_session.add(attempt)
    await db_session.commit()  # no error

    fetched = (await db_session.execute(select(DecisionReasonRow))).scalar_one()
    assert fetched.field_path == field_path


@pytest.mark.parametrize(
    "bad_path",
    [
        "line_items.00.quantity",
        "Total_Amount",
        "line_items..x",
        "line_items.1",
        "bogus_field",
        "line_items.1.bogus_field",
        "",
    ],
)
async def test_reason_field_path_shape_is_enforced(
    db_session: AsyncSession, bad_path: str
) -> None:
    validation = await _make_validation(db_session)
    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.ACCEPTED,
    )
    attempt.reasons = [_manual_reason(0, field_path=bad_path)]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


# --- source_rule / source_finding_id agree with code --------------------


async def test_manual_review_reason_with_null_source_rule_and_finding_is_valid(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.NEEDS_REVIEW,
    )
    attempt.reasons = [_manual_reason(0)]
    db_session.add(attempt)
    await db_session.commit()  # no error


async def test_manual_review_reason_rejects_a_source_rule(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    finding = _validation_finding(0)
    await _attach_finding(db_session, validation, finding)

    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.NEEDS_REVIEW,
    )
    attempt.reasons = [
        _manual_reason(0, source_rule="totals_do_not_reconcile")
    ]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_manual_review_reason_rejects_a_source_finding_id(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    finding = _validation_finding(0)
    await _attach_finding(db_session, validation, finding)

    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.NEEDS_REVIEW,
    )
    attempt.reasons = [
        _manual_reason(0, source_finding_id=finding.validation_finding_id)
    ]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_rule_derived_reason_requires_a_source_rule(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    finding = _validation_finding(0)
    await _attach_finding(db_session, validation, finding)

    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.NEEDS_REVIEW,
    )
    attempt.reasons = [
        _reason(0, finding_id=finding.validation_finding_id, source_rule=None)
    ]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_rule_derived_reason_requires_a_source_finding_id(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.NEEDS_REVIEW,
    )
    attempt.reasons = [_reason(0, finding_id=None)]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_rule_derived_reason_requires_source_rule_to_match_code(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    finding = _validation_finding(0)
    await _attach_finding(db_session, validation, finding)

    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.NEEDS_REVIEW,
    )
    attempt.reasons = [
        _reason(
            0,
            finding_id=finding.validation_finding_id,
            code=DecisionReasonCode.NO_LINE_ITEMS,
            source_rule="totals_do_not_reconcile",
        )
    ]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.parametrize("rule", list(ValidationRule), ids=lambda r: r.value)
async def test_every_rule_can_back_a_matching_reason(
    db_session: AsyncSession, rule: ValidationRule
) -> None:
    validation = await _make_validation(db_session)
    finding = _validation_finding(0, rule=rule)
    await _attach_finding(db_session, validation, finding)

    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.NEEDS_REVIEW,
    )
    attempt.reasons = [
        _reason(
            0,
            finding_id=finding.validation_finding_id,
            code=DecisionReasonCode(rule.value),
            triggers_review=True,
            source_rule=rule.value,
            message="x.",
        )
    ]
    db_session.add(attempt)
    await db_session.commit()  # no error


# --- cascade deletes -----------------------------------------------------


async def test_deleting_document_cascades_to_decision_rows(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    normalization = await db_session.get(
        NormalizationAttempt, validation.normalization_id
    )
    extraction = await db_session.get(ExtractionAttempt, normalization.extraction_id)
    finding = _validation_finding(0)
    await _attach_finding(db_session, validation, finding)

    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.NEEDS_REVIEW,
    )
    attempt.reasons = [_reason(0, finding_id=finding.validation_finding_id)]
    db_session.add(attempt)
    await db_session.commit()

    doc = await db_session.get(Document, extraction.document_id)
    await db_session.delete(doc)
    await db_session.commit()

    assert (await db_session.execute(select(DecisionAttempt))).first() is None
    assert (await db_session.execute(select(DecisionReasonRow))).first() is None


async def test_deleting_a_referenced_validation_finding_is_rejected(
    db_session: AsyncSession,
) -> None:
    validation = await _make_validation(db_session)
    finding = _validation_finding(0)
    await _attach_finding(db_session, validation, finding)

    attempt = _decision(
        validation.validation_id,
        status=DecisionStatus.COMPLETED,
        outcome=DecisionOutcome.NEEDS_REVIEW,
    )
    attempt.reasons = [
        _reason(0, finding_id=finding.validation_finding_id),
        _manual_reason(1),
    ]
    db_session.add(attempt)
    await db_session.commit()

    await db_session.delete(finding)
    with pytest.raises(IntegrityError):
        await db_session.commit()

    # A completed decision is an audit record. Its source finding must not be
    # removable independently, because doing so would silently erase a reason
    # while leaving the stored outcome unchanged. Deleting the owning document
    # remains valid and cascades through both complete branches (covered above).


# --- enum columns persist public values ---------------------------------


def test_status_enum_persists_public_values() -> None:
    enum_type = DecisionAttempt.__table__.c.status.type
    assert enum_type.enums == [member.value for member in DecisionStatus]


def test_outcome_enum_persists_public_values() -> None:
    enum_type = DecisionAttempt.__table__.c.outcome.type
    assert enum_type.enums == [member.value for member in DecisionOutcome]


def test_code_enum_persists_public_values() -> None:
    enum_type = DecisionReasonRow.__table__.c.code.type
    assert enum_type.enums == [member.value for member in DecisionReasonCode]
