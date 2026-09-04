"""Tests for the Stage 5 validation persistence models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
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


async def _make_normalization(session: AsyncSession) -> NormalizationAttempt:
    """A COMPLETED normalization attempt with its document + extraction chain."""
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
    return normalization


def _validation(
    normalization_id: uuid.UUID, *, number: int = 1, **kw
) -> ValidationAttempt:
    status = kw.pop("status", ValidationStatus.PROCESSING)
    if status is not ValidationStatus.PROCESSING:
        kw.setdefault("completed_at", datetime.now(timezone.utc))
    if status is ValidationStatus.FAILED:
        kw.setdefault("failure_code", "VALIDATION_FAILED")
        kw.setdefault("failure_message", "Validation failed.")
    return ValidationAttempt(
        normalization_id=normalization_id,
        attempt_number=number,
        status=status,
        **kw,
    )


def _finding(position: int, **kw) -> ValidationFindingRow:
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


async def test_create_attempt_with_findings_and_relationships(
    db_session: AsyncSession,
) -> None:
    normalization = await _make_normalization(db_session)
    attempt = _validation(
        normalization.normalization_id, status=ValidationStatus.COMPLETED
    )
    attempt.findings = [
        _finding(
            0,
            rule=ValidationRule.MISSING_REQUIRED_FIELD,
            severity=FindingSeverity.ERROR,
            field_path="currency",
            expected=None,
            actual=None,
            message="A field required to process this invoice is missing.",
            context={},
        ),
        _finding(1, field_path="line_items.2.line_total"),
        _finding(
            2,
            rule=ValidationRule.HIGH_VALUE_INVOICE,
            severity=FindingSeverity.INFO,
            field_path=None,
            expected=None,
            actual=None,
            message="The invoice total meets or exceeds the high-value threshold.",
            context={"threshold": 10000, "currency": "EUR"},
        ),
    ]
    db_session.add(attempt)
    await db_session.commit()

    fetched = (
        await db_session.execute(
            select(ValidationAttempt).where(
                ValidationAttempt.normalization_id == normalization.normalization_id
            )
        )
    ).scalar_one()
    assert fetched.attempt_number == 1
    assert fetched.status is ValidationStatus.COMPLETED
    assert fetched.completed_at is not None
    assert fetched.failure_code is None
    assert [f.position for f in fetched.findings] == [0, 1, 2]
    assert fetched.findings[0].rule is ValidationRule.MISSING_REQUIRED_FIELD
    assert fetched.findings[0].severity is FindingSeverity.ERROR
    assert fetched.findings[1].field_path == "line_items.2.line_total"
    assert fetched.findings[2].context == {"threshold": 10000, "currency": "EUR"}

    await db_session.refresh(normalization, ["validations"])
    assert [v.validation_id for v in normalization.validations] == [
        fetched.validation_id
    ]
    assert fetched.source_normalization.normalization_id == normalization.normalization_id


async def test_multiple_attempts_per_normalization_are_allowed(
    db_session: AsyncSession,
) -> None:
    normalization = await _make_normalization(db_session)
    db_session.add_all(
        [
            _validation(
                normalization.normalization_id,
                number=1,
                status=ValidationStatus.FAILED,
            ),
            _validation(
                normalization.normalization_id,
                number=2,
                status=ValidationStatus.COMPLETED,
            ),
        ]
    )
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(ValidationAttempt.attempt_number)
                .where(
                    ValidationAttempt.normalization_id
                    == normalization.normalization_id
                )
                .order_by(ValidationAttempt.attempt_number)
            )
        )
        .scalars()
        .all()
    )
    assert rows == [1, 2]


async def test_attempt_number_is_unique_per_normalization(
    db_session: AsyncSession,
) -> None:
    normalization = await _make_normalization(db_session)
    db_session.add(
        _validation(
            normalization.normalization_id, number=1, status=ValidationStatus.FAILED
        )
    )
    await db_session.commit()

    db_session.add(_validation(normalization.normalization_id, number=1))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_only_one_active_attempt_per_normalization(
    db_session: AsyncSession,
) -> None:
    normalization = await _make_normalization(db_session)
    db_session.add(
        _validation(
            normalization.normalization_id,
            number=1,
            status=ValidationStatus.PROCESSING,
        )
    )
    await db_session.commit()

    db_session.add(
        _validation(
            normalization.normalization_id,
            number=2,
            status=ValidationStatus.PROCESSING,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_second_non_active_attempt_is_fine(db_session: AsyncSession) -> None:
    normalization = await _make_normalization(db_session)
    db_session.add(
        _validation(
            normalization.normalization_id,
            number=1,
            status=ValidationStatus.PROCESSING,
        )
    )
    await db_session.commit()
    db_session.add(
        _validation(
            normalization.normalization_id,
            number=2,
            status=ValidationStatus.FAILED,
        )
    )
    await db_session.commit()  # no error


@pytest.mark.parametrize(
    ("status", "fields"),
    [
        (ValidationStatus.PROCESSING, {"completed_at": datetime.now(timezone.utc)}),
        (ValidationStatus.PROCESSING, {"failure_code": "EARLY"}),
        (ValidationStatus.COMPLETED, {"completed_at": None}),
        (ValidationStatus.COMPLETED, {"failure_message": "should not be here"}),
        (ValidationStatus.FAILED, {"completed_at": None}),
        (ValidationStatus.FAILED, {"failure_code": None}),
        (ValidationStatus.FAILED, {"failure_message": None}),
    ],
)
async def test_status_dependent_fields_are_enforced(
    db_session: AsyncSession,
    status: ValidationStatus,
    fields: dict,
) -> None:
    normalization = await _make_normalization(db_session)
    db_session.add(
        _validation(normalization.normalization_id, status=status, **fields)
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_attempt_number_must_be_positive(db_session: AsyncSession) -> None:
    normalization = await _make_normalization(db_session)
    db_session.add(
        _validation(
            normalization.normalization_id,
            number=0,
            status=ValidationStatus.COMPLETED,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_finding_position_must_be_unique_and_non_negative(
    db_session: AsyncSession,
) -> None:
    normalization = await _make_normalization(db_session)
    attempt = _validation(
        normalization.normalization_id, status=ValidationStatus.COMPLETED
    )
    attempt.findings = [_finding(0), _finding(0)]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_finding_negative_position_is_rejected(db_session: AsyncSession) -> None:
    normalization = await _make_normalization(db_session)
    attempt = _validation(
        normalization.normalization_id, status=ValidationStatus.COMPLETED
    )
    attempt.findings = [_finding(-1)]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


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
async def test_finding_field_path_accepts_null_and_stage4_paths(
    db_session: AsyncSession, field_path: str | None
) -> None:
    normalization = await _make_normalization(db_session)
    attempt = _validation(
        normalization.normalization_id, status=ValidationStatus.COMPLETED
    )
    attempt.findings = [_finding(0, field_path=field_path)]
    db_session.add(attempt)
    await db_session.commit()  # no error

    fetched = (await db_session.execute(select(ValidationFindingRow))).scalar_one()
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
async def test_finding_field_path_shape_is_enforced(
    db_session: AsyncSession, bad_path: str
) -> None:
    normalization = await _make_normalization(db_session)
    attempt = _validation(
        normalization.normalization_id, status=ValidationStatus.COMPLETED
    )
    attempt.findings = [_finding(0, field_path=bad_path)]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_finding_context_jsonb_round_trips(db_session: AsyncSession) -> None:
    normalization = await _make_normalization(db_session)
    attempt = _validation(
        normalization.normalization_id, status=ValidationStatus.COMPLETED
    )
    context = {
        "matches": [
            {"document_id": str(uuid.uuid4()), "normalization_id": str(uuid.uuid4())}
        ],
        "tolerance": "0.01",
        "line_count": 3,
    }
    attempt.findings = [
        _finding(
            0,
            rule=ValidationRule.PROBABLE_DUPLICATE_INVOICE,
            expected=None,
            actual=None,
            message="This invoice appears to duplicate another invoice already in the system.",
            context=context,
        )
    ]
    db_session.add(attempt)
    await db_session.commit()

    fetched = (await db_session.execute(select(ValidationFindingRow))).scalar_one()
    assert fetched.context == context


async def test_contract_decimal_and_uuid_context_values_round_trip_as_json(
    db_session: AsyncSession,
) -> None:
    normalization = await _make_normalization(db_session)
    attempt = _validation(
        normalization.normalization_id, status=ValidationStatus.COMPLETED
    )
    document_id = uuid.uuid4()
    attempt.findings = [
        _finding(
            0,
            context={
                "delta": Decimal("-1.2300"),
                "document_id": document_id,
                "nested": [Decimal("0.010"), {"id": document_id}],
            },
        )
    ]
    db_session.add(attempt)
    await db_session.commit()

    fetched = (await db_session.execute(select(ValidationFindingRow))).scalar_one()
    await db_session.refresh(fetched, ["context"])
    assert fetched.context == {
        "delta": "-1.2300",
        "document_id": str(document_id),
        "nested": ["0.010", {"id": str(document_id)}],
    }


@pytest.mark.parametrize("context", [{"bad": 0.1}, {1: "bad"}])
async def test_context_jsonb_rejects_non_contract_json_values(
    db_session: AsyncSession, context: dict
) -> None:
    normalization = await _make_normalization(db_session)
    attempt = _validation(
        normalization.normalization_id, status=ValidationStatus.COMPLETED
    )
    attempt.findings = [_finding(0, context=context)]
    db_session.add(attempt)
    with pytest.raises((ValueError, StatementError)):
        await db_session.commit()


async def test_deleting_document_cascades_to_validation_rows(
    db_session: AsyncSession,
) -> None:
    normalization = await _make_normalization(db_session)
    extraction = await db_session.get(ExtractionAttempt, normalization.extraction_id)
    attempt = _validation(
        normalization.normalization_id, status=ValidationStatus.COMPLETED
    )
    attempt.findings = [_finding(0), _finding(1, field_path="total_amount")]
    db_session.add(attempt)
    await db_session.commit()

    doc = await db_session.get(Document, extraction.document_id)
    await db_session.delete(doc)
    await db_session.commit()

    assert (await db_session.execute(select(ValidationAttempt))).first() is None
    assert (await db_session.execute(select(ValidationFindingRow))).first() is None


def test_rule_enum_persists_public_values() -> None:
    enum_type = ValidationFindingRow.__table__.c.rule.type
    assert enum_type.enums == [member.value for member in ValidationRule]


def test_severity_enum_persists_public_values() -> None:
    enum_type = ValidationFindingRow.__table__.c.severity.type
    assert enum_type.enums == [member.value for member in FindingSeverity]
