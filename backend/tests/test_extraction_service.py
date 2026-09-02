"""Tests for the extraction repository/service foundation and the processing
lifecycle (Stage 3, steps 5 and 6).

These run against the real PostgreSQL test database (via the ``db_session``
fixture). No AI provider is involved - result producers are plain callables that
return a hand-built contract or raise.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError, NotFoundError
from app.models import Document, DocumentStatus, ExtractionAttempt, ExtractionStatus
from app.schemas.extraction import InvoiceExtraction
from app.schemas.extraction_api import InvoiceExtractionResult
from app.schemas.extraction_persistence import invoice_extraction_from_attempt
from app.services.processing.extraction import (
    ExtractionError,
    ExtractionRepository,
    ExtractionService,
)
from app.services.processing.extraction import lifecycle
from app.services.processing.extraction.service import _GENERIC_FAILURE

from tests._helpers import make_pdf


# --- fixtures / helpers --------------------------------------------------


def _field(value=None, confidence=None) -> dict:
    return {"value": value, "confidence": confidence}


def _contract() -> InvoiceExtraction:
    return InvoiceExtraction.model_validate(
        {
            "invoice_number": _field("INV-77", "0.98"),
            "invoice_date": _field("3 March 2026", "0.7"),
            "due_date": _field(None, None),
            "vendor_name": _field("Acme GmbH", "0.9"),
            "vendor_tax_id": _field("DE12345"),
            "customer_name": _field("Beta Ltd"),
            "currency": _field("eur", "1"),
            "subtotal": _field("100.00", "0.9"),
            "tax_amount": _field("19.00", "0.9"),
            "total_amount": _field("119.00", "0.95"),
            "line_items": [
                {
                    "description": _field("Widget", "0.99"),
                    "quantity": _field("2"),
                    "unit_price": _field("10.00"),
                    "line_total": _field("20.00"),
                },
                {
                    "description": _field("Gadget"),
                    "quantity": _field("1"),
                    "unit_price": _field("99.00"),
                    "line_total": _field("99.00"),
                },
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
    raise RuntimeError("provider socket exploded: host=10.0.0.4 key=sk-secret")


def _service(session: AsyncSession) -> ExtractionService:
    return ExtractionService(session)


# --- happy path --------------------------------------------------------- --


async def test_start_completes_and_persists_all_fields(db_session: AsyncSession) -> None:
    doc = await _make_document(db_session)
    doc_id = doc.document_id

    attempt = await _service(db_session).start(
        doc_id, produce=_ok, provider_name="fake", provider_model="fake-v1"
    )
    extraction_id = attempt.extraction_id

    assert attempt.status is ExtractionStatus.COMPLETED
    assert attempt.attempt_number == 1
    assert attempt.started_at is not None and attempt.completed_at is not None
    assert attempt.failure_code is None and attempt.failure_message is None
    assert attempt.invoice_number_value == "INV-77"
    assert attempt.invoice_number_confidence == Decimal("0.98000")
    assert attempt.currency_value == "EUR"  # upper-cased by the contract
    assert [li.position for li in attempt.line_items] == [0, 1]

    db_session.expire_all()
    reloaded = await db_session.get(Document, doc_id)
    assert reloaded.status is DocumentStatus.COMPLETED
    stored = await ExtractionRepository(db_session).get(extraction_id)
    assert invoice_extraction_from_attempt(stored) == _contract()


async def test_processing_state_is_durable_before_the_producer_runs(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc = await _make_document(db_session)
    seen: dict[str, object] = {}

    async def produce(document: Document) -> InvoiceExtraction:
        # The document handed in is already PROCESSING...
        seen["arg_status"] = document.status
        # ...and an independent session can see the committed PROCESSING attempt.
        async with session_factory() as other:
            row = (
                await other.execute(
                    select(ExtractionAttempt).where(
                        ExtractionAttempt.document_id == document.document_id
                    )
                )
            ).scalar_one()
            seen["row_status"] = row.status
            seen["doc_status"] = (await other.get(Document, document.document_id)).status
        return _contract()

    await _service(db_session).start(doc.document_id, produce=produce, provider_name="fake")

    assert seen["arg_status"] is DocumentStatus.PROCESSING
    assert seen["row_status"] is ExtractionStatus.PROCESSING
    assert seen["doc_status"] is DocumentStatus.PROCESSING


async def test_raw_response_is_persisted_but_never_in_the_public_result(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    attempt = await _service(db_session).start(
        doc.document_id,
        produce=_ok,
        provider_name="fake",
        raw_response={"provider_prose": "i think this is an invoice"},
    )

    assert attempt.raw_response == {"provider_prose": "i think this is an invoice"}
    dumped = InvoiceExtractionResult.from_attempt(attempt).model_dump_json()
    assert "provider_prose" not in dumped and "raw_response" not in dumped


# --- not found / conflicts ------------------------------------------------


async def test_start_on_unknown_document_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError) as excinfo:
        await _service(db_session).start(
            uuid.uuid4(), produce=_ok, provider_name="fake"
        )
    assert excinfo.value.code == "DOCUMENT_NOT_FOUND"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (DocumentStatus.PROCESSING, "EXTRACTION_IN_PROGRESS"),
        (DocumentStatus.COMPLETED, "DOCUMENT_ALREADY_EXTRACTED"),
        (DocumentStatus.FAILED, "EXTRACTION_NOT_FAILED"),
        (DocumentStatus.NEEDS_REVIEW, "DOCUMENT_NOT_EXTRACTABLE"),
    ],
)
async def test_start_rejects_documents_that_are_not_uploaded(
    db_session: AsyncSession, status: DocumentStatus, code: str
) -> None:
    doc = await _make_document(db_session, status=status)
    doc_id = doc.document_id
    with pytest.raises(ConflictError) as excinfo:
        await _service(db_session).start(doc_id, produce=_ok, provider_name="fake")
    assert excinfo.value.code == code
    db_session.expire_all()
    assert (await db_session.get(Document, doc_id)).status is status


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (DocumentStatus.UPLOADED, "EXTRACTION_NOT_FAILED"),
        (DocumentStatus.COMPLETED, "EXTRACTION_NOT_FAILED"),
        (DocumentStatus.PROCESSING, "EXTRACTION_IN_PROGRESS"),
    ],
)
async def test_retry_rejects_documents_whose_extraction_did_not_fail(
    db_session: AsyncSession, status: DocumentStatus, code: str
) -> None:
    doc = await _make_document(db_session, status=status)
    with pytest.raises(ConflictError) as excinfo:
        await _service(db_session).retry(doc.document_id, produce=_ok, provider_name="fake")
    assert excinfo.value.code == code


async def test_a_stale_active_attempt_blocks_a_new_run(db_session: AsyncSession) -> None:
    # Document says FAILED but a PROCESSING attempt is still on record.
    doc = await _make_document(db_session, status=DocumentStatus.FAILED)
    db_session.add(
        ExtractionAttempt(
            document_id=doc.document_id,
            attempt_number=1,
            status=ExtractionStatus.PROCESSING,
            provider_name="fake",
        )
    )
    await db_session.commit()

    with pytest.raises(ConflictError) as excinfo:
        await _service(db_session).retry(doc.document_id, produce=_ok, provider_name="fake")
    assert excinfo.value.code == "EXTRACTION_IN_PROGRESS"


async def test_concurrent_start_is_rejected_while_first_producer_runs(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    doc = await _make_document(db_session)
    document_id = doc.document_id
    producer_started = asyncio.Event()
    allow_completion = asyncio.Event()

    async def paused(_doc: Document) -> InvoiceExtraction:
        producer_started.set()
        await allow_completion.wait()
        return _contract()

    first = asyncio.create_task(
        _service(db_session).start(document_id, produce=paused, provider_name="fake")
    )
    await producer_started.wait()
    try:
        async with session_factory() as concurrent_session:
            with pytest.raises(ConflictError) as excinfo:
                await _service(concurrent_session).start(
                    document_id, produce=_ok, provider_name="fake"
                )
            assert excinfo.value.code == "EXTRACTION_IN_PROGRESS"
    finally:
        allow_completion.set()

    completed = await first
    assert completed.status is ExtractionStatus.COMPLETED


# --- failure handling ------------------------------------------------- ----


async def test_producer_exception_marks_failed_with_a_safe_message(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    doc_id = doc.document_id

    attempt = await _service(db_session).start(doc_id, produce=_boom, provider_name="fake")

    assert attempt.status is ExtractionStatus.FAILED
    assert attempt.failure_code == "EXTRACTION_FAILED"
    assert attempt.failure_message == _GENERIC_FAILURE
    # No internal detail from the raised exception leaked into the row.
    assert "sk-secret" not in (attempt.failure_message or "")
    assert attempt.completed_at is not None
    assert attempt.line_items == []
    assert attempt.invoice_number_value is None and attempt.total_amount_value is None

    db_session.expire_all()
    assert (await db_session.get(Document, doc_id)).status is DocumentStatus.FAILED


async def test_extraction_error_keeps_its_own_safe_code_and_message(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)

    async def rate_limited(_doc: Document) -> InvoiceExtraction:
        raise ExtractionError("The extraction provider is rate limiting requests.", code="RATE_LIMITED")

    attempt = await _service(db_session).start(
        doc.document_id, produce=rate_limited, provider_name="fake"
    )

    assert attempt.status is ExtractionStatus.FAILED
    assert attempt.failure_code == "RATE_LIMITED"
    assert attempt.failure_message == "The extraction provider is rate limiting requests."


async def test_schema_invalid_producer_output_marks_failed_and_stores_nothing(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    doc_id = doc.document_id

    async def malformed(_doc: Document) -> dict:
        # confidence out of range - not a valid contract
        return {"invoice_number": {"value": "x", "confidence": "3"}, "note": "looks invoicey"}

    attempt = await _service(db_session).start(doc_id, produce=malformed, provider_name="fake")

    assert attempt.status is ExtractionStatus.FAILED
    assert attempt.failure_code == "MALFORMED_EXTRACTION"
    assert attempt.invoice_number_value is None
    assert attempt.line_items == []
    db_session.expire_all()
    assert (await db_session.get(Document, doc_id)).status is DocumentStatus.FAILED


async def test_unsafely_constructed_contract_is_revalidated_and_fails_cleanly(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    document_id = doc.document_id

    async def unsafe(_doc: Document) -> InvoiceExtraction:
        return InvoiceExtraction.model_construct()

    attempt = await _service(db_session).start(
        document_id, produce=unsafe, provider_name="fake"
    )

    assert attempt.status is ExtractionStatus.FAILED
    assert attempt.failure_code == "MALFORMED_EXTRACTION"
    assert attempt.line_items == []
    db_session.expire_all()
    assert (await db_session.get(Document, document_id)).status is DocumentStatus.FAILED


async def test_original_pdf_is_untouched_by_a_failed_extraction(
    db_session: AsyncSession, storage
) -> None:
    doc = await _make_document(db_session)
    doc_id = doc.document_id
    pdf = make_pdf(2)
    stored = await storage.save_bytes(doc_id, pdf)
    doc.file_location = stored.location
    await db_session.commit()

    await _service(db_session).start(doc_id, produce=_boom, provider_name="fake")

    assert await storage.exists(stored.location)
    assert (await storage.path_for(stored.location)).read_bytes() == pdf
    db_session.expire_all()
    reloaded = await db_session.get(Document, doc_id)
    assert reloaded.file_location == stored.location
    assert reloaded.file_hash == "a" * 64
    assert reloaded.file_size_bytes == 2048 and reloaded.page_count == 2


# --- retry + attempt history --------------------------------------------


async def test_retry_after_failure_creates_a_new_attempt_and_completes(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    doc_id = doc.document_id
    svc = _service(db_session)

    first = await svc.start(doc_id, produce=_boom, provider_name="fake")
    assert first.status is ExtractionStatus.FAILED

    second = await svc.retry(doc_id, produce=_ok, provider_name="fake")
    assert second.status is ExtractionStatus.COMPLETED
    assert second.attempt_number == 2

    db_session.expire_all()
    assert (await db_session.get(Document, doc_id)).status is DocumentStatus.COMPLETED

    history = await ExtractionRepository(db_session).list_for_document(doc_id)
    assert [(a.attempt_number, a.status) for a in history] == [
        (1, ExtractionStatus.FAILED),
        (2, ExtractionStatus.COMPLETED),
    ]
    # The first attempt was not mutated by the retry.
    assert history[0].failure_code == "EXTRACTION_FAILED"
    assert history[0].completed_at is not None


async def test_retry_requires_an_actual_failed_attempt(db_session: AsyncSession) -> None:
    doc = await _make_document(db_session, status=DocumentStatus.FAILED)

    with pytest.raises(ConflictError) as excinfo:
        await _service(db_session).retry(
            doc.document_id, produce=_ok, provider_name="fake"
        )

    assert excinfo.value.code == "EXTRACTION_NOT_FAILED"
    assert await ExtractionRepository(db_session).list_for_document(doc.document_id) == []


async def test_attempt_history_is_preserved_across_failures_then_success(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    svc = _service(db_session)

    await svc.start(doc.document_id, produce=_boom, provider_name="fake")
    await svc.retry(doc.document_id, produce=_boom, provider_name="fake")
    await svc.retry(doc.document_id, produce=_ok, provider_name="fake")

    history = await ExtractionRepository(db_session).list_for_document(doc.document_id)
    assert [(a.attempt_number, a.status) for a in history] == [
        (1, ExtractionStatus.FAILED),
        (2, ExtractionStatus.FAILED),
        (3, ExtractionStatus.COMPLETED),
    ]
    assert history[0].failure_code == "EXTRACTION_FAILED"
    assert history[1].failure_code == "EXTRACTION_FAILED"
    # Only the successful attempt carries extracted data.
    assert history[0].total_amount_value is None
    assert history[2].total_amount_value == Decimal("119.00")
    assert [li.position for li in history[2].line_items] == [0, 1]


async def test_latest_for_document_tracks_the_newest_attempt(
    db_session: AsyncSession,
) -> None:
    doc = await _make_document(db_session)
    repo = ExtractionRepository(db_session)
    svc = _service(db_session)

    assert await repo.latest_for_document(doc.document_id) is None
    await svc.start(doc.document_id, produce=_boom, provider_name="fake")
    await svc.retry(doc.document_id, produce=_ok, provider_name="fake")

    latest = await repo.latest_for_document(doc.document_id)
    assert latest.attempt_number == 2 and latest.status is ExtractionStatus.COMPLETED


# --- repository unit-ish -----------------------------------------------


async def test_next_attempt_number_counts_from_one(db_session: AsyncSession) -> None:
    doc = await _make_document(db_session)
    repo = ExtractionRepository(db_session)

    assert await repo.next_attempt_number(doc.document_id) == 1
    await _service(db_session).start(doc.document_id, produce=_boom, provider_name="fake")
    assert await repo.next_attempt_number(doc.document_id) == 2


async def test_get_for_document_scopes_by_document(db_session: AsyncSession) -> None:
    doc_a = await _make_document(db_session)
    doc_b = await _make_document(db_session)
    attempt = await _service(db_session).start(doc_a.document_id, produce=_ok, provider_name="fake")

    repo = ExtractionRepository(db_session)
    assert await repo.get_for_document(doc_a.document_id, attempt.extraction_id) is not None
    assert await repo.get_for_document(doc_b.document_id, attempt.extraction_id) is None


# --- lifecycle guards -------------------------------------------------


def test_lifecycle_attempt_transition_table() -> None:
    lifecycle.ensure_attempt_transition(ExtractionStatus.PROCESSING, ExtractionStatus.COMPLETED)
    lifecycle.ensure_attempt_transition(ExtractionStatus.PROCESSING, ExtractionStatus.FAILED)
    for bad in (
        (ExtractionStatus.PROCESSING, ExtractionStatus.PROCESSING),
        (ExtractionStatus.COMPLETED, ExtractionStatus.FAILED),
        (ExtractionStatus.FAILED, ExtractionStatus.COMPLETED),
        (ExtractionStatus.COMPLETED, ExtractionStatus.PROCESSING),
    ):
        with pytest.raises(ValueError):
            lifecycle.ensure_attempt_transition(*bad)


def test_lifecycle_never_allows_needs_review() -> None:
    assert DocumentStatus.NEEDS_REVIEW not in lifecycle.START_FROM
    assert DocumentStatus.NEEDS_REVIEW not in lifecycle.RETRY_FROM
    with pytest.raises(ConflictError):
        lifecycle.ensure_document_can_extract(DocumentStatus.NEEDS_REVIEW, action="start")
    with pytest.raises(ConflictError):
        lifecycle.ensure_document_can_extract(DocumentStatus.NEEDS_REVIEW, action="retry")
