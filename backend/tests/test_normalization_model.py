"""Tests for the Stage 4 normalization persistence models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Document,
    ExtractionAttempt,
    ExtractionStatus,
    NormalizationAttempt,
    NormalizationErrorCode,
    NormalizationFieldError,
    NormalizationLineItem,
    NormalizationStatus,
)


async def _make_extraction(session: AsyncSession) -> ExtractionAttempt:
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
    return extraction


def _attempt(
    extraction_id: uuid.UUID, *, number: int = 1, **kw
) -> NormalizationAttempt:
    status = kw.pop("status", NormalizationStatus.PROCESSING)
    if status is not NormalizationStatus.PROCESSING:
        kw.setdefault("completed_at", datetime.now(timezone.utc))
    if status is NormalizationStatus.FAILED:
        kw.setdefault("failure_code", "NORMALIZATION_FAILED")
        kw.setdefault("failure_message", "Normalization failed.")
    return NormalizationAttempt(
        extraction_id=extraction_id,
        attempt_number=number,
        status=status,
        **kw,
    )


async def test_create_attempt_with_line_items_errors_and_relationships(
    db_session: AsyncSession,
) -> None:
    extraction = await _make_extraction(db_session)
    attempt = _attempt(
        extraction.extraction_id,
        status=NormalizationStatus.COMPLETED,
        invoice_number="INV-1",
        invoice_date="2026-01-15",
        due_date=None,
        currency="EUR",
        total_amount=Decimal("119.00"),
    )
    attempt.line_items = [
        NormalizationLineItem(position=0, description="Widget", quantity=Decimal("2")),
        NormalizationLineItem(position=1, description="Gadget", line_total=Decimal("9.5")),
    ]
    attempt.errors = [
        NormalizationFieldError(
            field_path="due_date",
            raw_value="31/02/2026",
            code=NormalizationErrorCode.INVALID_DATE,
            message="The invoice date could not be recognized in a supported format.",
        )
    ]
    db_session.add(attempt)
    await db_session.commit()

    fetched = (
        await db_session.execute(
            select(NormalizationAttempt).where(
                NormalizationAttempt.extraction_id == extraction.extraction_id
            )
        )
    ).scalar_one()
    assert fetched.attempt_number == 1
    assert fetched.status is NormalizationStatus.COMPLETED
    assert fetched.completed_at is not None
    assert fetched.failure_code is None
    assert fetched.invoice_number == "INV-1"
    assert fetched.invoice_date == "2026-01-15"
    assert fetched.total_amount == Decimal("119.00")
    assert [li.position for li in fetched.line_items] == [0, 1]
    assert fetched.errors[0].code is NormalizationErrorCode.INVALID_DATE
    assert fetched.errors[0].raw_value == "31/02/2026"

    await db_session.refresh(extraction, ["normalizations"])
    assert [n.normalization_id for n in extraction.normalizations] == [
        fetched.normalization_id
    ]
    assert fetched.source_extraction.extraction_id == extraction.extraction_id


async def test_multiple_attempts_per_extraction_are_allowed(
    db_session: AsyncSession,
) -> None:
    extraction = await _make_extraction(db_session)
    db_session.add_all(
        [
            _attempt(extraction.extraction_id, number=1, status=NormalizationStatus.FAILED),
            _attempt(extraction.extraction_id, number=2, status=NormalizationStatus.COMPLETED),
        ]
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(NormalizationAttempt.attempt_number)
            .where(NormalizationAttempt.extraction_id == extraction.extraction_id)
            .order_by(NormalizationAttempt.attempt_number)
        )
    ).scalars().all()
    assert rows == [1, 2]


async def test_attempt_number_is_unique_per_extraction(db_session: AsyncSession) -> None:
    extraction = await _make_extraction(db_session)
    db_session.add(
        _attempt(extraction.extraction_id, number=1, status=NormalizationStatus.FAILED)
    )
    await db_session.commit()

    db_session.add(_attempt(extraction.extraction_id, number=1))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_only_one_active_attempt_per_extraction(db_session: AsyncSession) -> None:
    extraction = await _make_extraction(db_session)
    db_session.add(
        _attempt(extraction.extraction_id, number=1, status=NormalizationStatus.PROCESSING)
    )
    await db_session.commit()

    db_session.add(
        _attempt(extraction.extraction_id, number=2, status=NormalizationStatus.PROCESSING)
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_second_non_active_attempt_is_fine(db_session: AsyncSession) -> None:
    extraction = await _make_extraction(db_session)
    db_session.add(
        _attempt(extraction.extraction_id, number=1, status=NormalizationStatus.PROCESSING)
    )
    await db_session.commit()
    db_session.add(
        _attempt(extraction.extraction_id, number=2, status=NormalizationStatus.FAILED)
    )
    await db_session.commit()  # no error


@pytest.mark.parametrize(
    ("status", "fields"),
    [
        (NormalizationStatus.PROCESSING, {"completed_at": datetime.now(timezone.utc)}),
        (NormalizationStatus.PROCESSING, {"failure_code": "EARLY"}),
        (NormalizationStatus.COMPLETED, {"completed_at": None}),
        (NormalizationStatus.COMPLETED, {"failure_message": "should not be here"}),
        (NormalizationStatus.FAILED, {"completed_at": None}),
        (NormalizationStatus.FAILED, {"failure_code": None}),
        (NormalizationStatus.FAILED, {"failure_message": None}),
    ],
)
async def test_status_dependent_fields_are_enforced(
    db_session: AsyncSession,
    status: NormalizationStatus,
    fields: dict,
) -> None:
    extraction = await _make_extraction(db_session)
    db_session.add(_attempt(extraction.extraction_id, status=status, **fields))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.parametrize("bad_date", ["2026-1-5", "15/01/2026", "20260115", "not-a-date"])
async def test_non_iso_date_shape_is_rejected(
    db_session: AsyncSession, bad_date: str
) -> None:
    extraction = await _make_extraction(db_session)
    db_session.add(
        _attempt(
            extraction.extraction_id,
            status=NormalizationStatus.COMPLETED,
            invoice_date=bad_date,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.parametrize("bad_currency", ["eur", "EU", "EURO", "12"])
async def test_currency_must_be_upper_alpha3(
    db_session: AsyncSession, bad_currency: str
) -> None:
    extraction = await _make_extraction(db_session)
    db_session.add(
        _attempt(
            extraction.extraction_id,
            status=NormalizationStatus.COMPLETED,
            currency=bad_currency,
        )
    )
    # bad case / short / non-alpha -> CHECK violation (IntegrityError);
    # "EURO" -> varchar(3) truncation (DBAPIError). IntegrityError is a DBAPIError.
    with pytest.raises(DBAPIError):
        await db_session.commit()


async def test_overlong_identifier_is_rejected_by_the_column(
    db_session: AsyncSession,
) -> None:
    extraction = await _make_extraction(db_session)
    db_session.add(
        _attempt(
            extraction.extraction_id,
            status=NormalizationStatus.COMPLETED,
            invoice_number="X" * 101,
        )
    )
    with pytest.raises(DBAPIError):
        await db_session.commit()


async def test_line_item_position_must_be_unique_and_non_negative(
    db_session: AsyncSession,
) -> None:
    extraction = await _make_extraction(db_session)
    attempt = _attempt(extraction.extraction_id)
    attempt.line_items = [
        NormalizationLineItem(position=0),
        NormalizationLineItem(position=0),
    ]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_at_most_one_error_per_field_path(db_session: AsyncSession) -> None:
    extraction = await _make_extraction(db_session)
    attempt = _attempt(extraction.extraction_id, status=NormalizationStatus.COMPLETED)
    attempt.errors = [
        NormalizationFieldError(
            field_path="total_amount",
            raw_value="1,2,3",
            code=NormalizationErrorCode.INVALID_NUMBER,
            message="bad",
        ),
        NormalizationFieldError(
            field_path="total_amount",
            raw_value="4,5,6",
            code=NormalizationErrorCode.AMBIGUOUS_NUMBER,
            message="also bad",
        ),
    ]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.parametrize(
    "bad_path",
    [
        "line_items.00.quantity",
        "Total_Amount",
        "line_items..x",
        "line_items.1",
        "bogus_field",
        "line_items.1.bogus_field",
    ],
)
async def test_error_field_path_shape_is_enforced(
    db_session: AsyncSession, bad_path: str
) -> None:
    extraction = await _make_extraction(db_session)
    attempt = _attempt(extraction.extraction_id, status=NormalizationStatus.COMPLETED)
    attempt.errors = [
        NormalizationFieldError(
            field_path=bad_path,
            raw_value=None,
            code=NormalizationErrorCode.INVALID_NUMBER,
            message="bad path",
        )
    ]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_deleting_document_cascades_to_normalization_rows(
    db_session: AsyncSession,
) -> None:
    extraction = await _make_extraction(db_session)
    attempt = _attempt(extraction.extraction_id, status=NormalizationStatus.COMPLETED)
    attempt.line_items = [NormalizationLineItem(position=0)]
    attempt.errors = [
        NormalizationFieldError(
            field_path="currency",
            raw_value="ZZZ",
            code=NormalizationErrorCode.UNKNOWN_CURRENCY,
            message="unknown",
        )
    ]
    db_session.add(attempt)
    await db_session.commit()

    doc = await db_session.get(Document, extraction.document_id)
    await db_session.delete(doc)
    await db_session.commit()

    assert (await db_session.execute(select(NormalizationAttempt))).first() is None
    assert (await db_session.execute(select(NormalizationLineItem))).first() is None
    assert (await db_session.execute(select(NormalizationFieldError))).first() is None


def test_error_enum_persists_public_values() -> None:
    enum_type = NormalizationFieldError.__table__.c.code.type
    assert enum_type.enums == [member.value for member in NormalizationErrorCode]
