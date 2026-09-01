"""Tests for the Stage 3 extraction persistence models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, ExtractionAttempt, ExtractionLineItem, ExtractionStatus


async def _make_document(session: AsyncSession) -> Document:
    doc = Document(
        original_filename="invoice.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="a" * 64,
        file_size_bytes=1234,
        page_count=1,
    )
    session.add(doc)
    await session.flush()
    return doc


def _attempt(document_id: uuid.UUID, *, number: int = 1, **kw) -> ExtractionAttempt:
    status = kw.pop("status", ExtractionStatus.PROCESSING)
    if status is not ExtractionStatus.PROCESSING:
        kw.setdefault("completed_at", datetime.now(timezone.utc))
    if status is ExtractionStatus.FAILED:
        kw.setdefault("failure_code", "EXTRACTION_FAILED")
    return ExtractionAttempt(
        document_id=document_id,
        attempt_number=number,
        status=status,
        provider_name=kw.pop("provider_name", "fake"),
        **kw,
    )


async def test_create_attempt_with_line_items_and_relationships(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    attempt = _attempt(
        doc.document_id,
        status=ExtractionStatus.COMPLETED,
        provider_name="fake",
        provider_model="fake-v1",
        invoice_number_value="INV-1",
        invoice_number_confidence=Decimal("0.97"),
        invoice_date_value="15 Jan 2026",  # raw, unparsed
        total_amount_value=Decimal("119.00"),
        total_amount_confidence=None,
        raw_response={"provider": "fake", "echo": True},
    )
    attempt.line_items = [
        ExtractionLineItem(position=0, description_value="Widget", quantity_value=Decimal("2")),
        ExtractionLineItem(position=1, description_value="Gadget", line_total_value=Decimal("9.5")),
    ]
    db_session.add(attempt)
    await db_session.commit()

    fetched = (
        await db_session.execute(select(ExtractionAttempt).where(ExtractionAttempt.document_id == doc.document_id))
    ).scalar_one()
    assert fetched.attempt_number == 1
    assert fetched.status is ExtractionStatus.COMPLETED
    assert fetched.completed_at is not None
    assert fetched.invoice_number_value == "INV-1"
    assert fetched.invoice_number_confidence == Decimal("0.97000")
    assert fetched.invoice_date_value == "15 Jan 2026"
    assert fetched.raw_response == {"provider": "fake", "echo": True}
    assert [li.position for li in fetched.line_items] == [0, 1]
    assert fetched.line_items[0].description_value == "Widget"

    await db_session.refresh(doc, ["extractions"])
    assert [a.extraction_id for a in doc.extractions] == [fetched.extraction_id]
    assert fetched.document.document_id == doc.document_id


async def test_multiple_attempts_per_document_are_allowed(db_session: AsyncSession) -> None:
    doc = await _make_document(db_session)
    db_session.add_all(
        [
            _attempt(doc.document_id, number=1, status=ExtractionStatus.FAILED),
            _attempt(doc.document_id, number=2, status=ExtractionStatus.COMPLETED),
        ]
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(ExtractionAttempt.attempt_number)
            .where(ExtractionAttempt.document_id == doc.document_id)
            .order_by(ExtractionAttempt.attempt_number)
        )
    ).scalars().all()
    assert rows == [1, 2]


async def test_attempt_number_is_unique_per_document(db_session: AsyncSession) -> None:
    doc = await _make_document(db_session)
    db_session.add(_attempt(doc.document_id, number=1, status=ExtractionStatus.FAILED))
    await db_session.commit()

    db_session.add(_attempt(doc.document_id, number=1))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_only_one_active_attempt_per_document(db_session: AsyncSession) -> None:
    doc = await _make_document(db_session)
    db_session.add(_attempt(doc.document_id, number=1, status=ExtractionStatus.PROCESSING))
    await db_session.commit()

    db_session.add(_attempt(doc.document_id, number=2, status=ExtractionStatus.PROCESSING))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_second_non_active_attempt_is_fine(db_session: AsyncSession) -> None:
    doc = await _make_document(db_session)
    db_session.add(_attempt(doc.document_id, number=1, status=ExtractionStatus.PROCESSING))
    await db_session.commit()
    db_session.add(_attempt(doc.document_id, number=2, status=ExtractionStatus.FAILED))
    await db_session.commit()  # no error


@pytest.mark.parametrize(
    ("status", "fields"),
    [
        (ExtractionStatus.PROCESSING, {"completed_at": datetime.now(timezone.utc)}),
        (ExtractionStatus.PROCESSING, {"failure_code": "EARLY_FAILURE"}),
        (ExtractionStatus.COMPLETED, {"completed_at": None}),
        (ExtractionStatus.COMPLETED, {"failure_message": "should not be here"}),
        (ExtractionStatus.FAILED, {"completed_at": None}),
        (ExtractionStatus.FAILED, {"failure_code": None}),
    ],
)
async def test_status_dependent_fields_are_enforced(
    db_session: AsyncSession,
    status: ExtractionStatus,
    fields: dict,
) -> None:
    doc = await _make_document(db_session)
    db_session.add(_attempt(doc.document_id, status=status, **fields))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_raw_extracted_text_is_not_artificially_length_limited(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    raw_date = "ambiguous-date-" * 100
    attempt = _attempt(
        doc.document_id,
        status=ExtractionStatus.COMPLETED,
        invoice_date_value=raw_date,
        vendor_name_value="vendor-" * 200,
    )
    db_session.add(attempt)
    await db_session.commit()

    assert attempt.invoice_date_value == raw_date
    assert attempt.vendor_name_value == "vendor-" * 200


async def test_confidence_columns_accept_null(db_session: AsyncSession) -> None:
    doc = await _make_document(db_session)
    attempt = _attempt(doc.document_id)
    # every confidence left as None
    db_session.add(attempt)
    await db_session.commit()
    assert attempt.total_amount_confidence is None


async def test_confidence_out_of_range_is_rejected(db_session: AsyncSession) -> None:
    doc = await _make_document(db_session)
    db_session.add(_attempt(doc.document_id, invoice_number_confidence=Decimal("1.5")))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_line_item_position_must_be_unique_and_non_negative(db_session: AsyncSession) -> None:
    doc = await _make_document(db_session)
    attempt = _attempt(doc.document_id)
    attempt.line_items = [
        ExtractionLineItem(position=0),
        ExtractionLineItem(position=0),
    ]
    db_session.add(attempt)
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_deleting_document_cascades_to_extractions_and_line_items(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    attempt = _attempt(doc.document_id)
    attempt.line_items = [ExtractionLineItem(position=0), ExtractionLineItem(position=1)]
    db_session.add(attempt)
    await db_session.commit()

    await db_session.delete(doc)
    await db_session.commit()

    assert (await db_session.execute(select(ExtractionAttempt))).first() is None
    assert (await db_session.execute(select(ExtractionLineItem))).first() is None
