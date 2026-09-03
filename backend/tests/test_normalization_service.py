"""Tests for the normalization repository/service, engine, and lifecycle
(Stage 4, step 11).

These run against the real PostgreSQL test database (via the ``db_session``
fixture). Normalization has no injected provider - the deterministic engine
:func:`normalize_extraction` is the whole "producer" - so a technical failure is
simulated by monkeypatching it (or ``apply_result``) to raise.

A completed Stage 3 extraction is set up by running the proven
:class:`ExtractionService` with a plain callable producer; no AI provider is
involved anywhere.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError, NotFoundError
from app.models import (
    Document,
    DocumentStatus,
    ExtractionAttempt,
    ExtractionStatus,
    NormalizationAttempt,
    NormalizationErrorCode,
    NormalizationFieldError,
    NormalizationLineItem,
    NormalizationStatus,
)
from app.schemas.extraction import InvoiceExtraction
from app.schemas.normalization import NORMALIZED_SCALAR_FIELD_NAMES
from app.services.processing.extraction import ExtractionRepository, ExtractionService
from app.services.processing.normalization import (
    NormalizationRepository,
    NormalizationService,
    normalize_extraction,
)
from app.services.processing.normalization import lifecycle
from app.services.processing.normalization.service import _GENERIC_FAILURE

_ENGINE_PATH = "app.services.processing.normalization.service.normalize_extraction"


# --- fixtures / helpers -------------------------------------------------


def _field(value=None, confidence=None) -> dict:
    return {"value": value, "confidence": confidence}


def _base_payload() -> dict:
    return {
        "invoice_number": _field("INV-77"),
        "invoice_date": _field("3 March 2026"),
        "due_date": _field(None),
        "vendor_name": _field("  Acme  GmbH  "),
        "vendor_tax_id": _field("DE 123 456 789"),
        "customer_name": _field("Beta Ltd"),
        "currency": _field("eur"),
        "subtotal": _field("100.00"),
        "tax_amount": _field("19.00"),
        "total_amount": _field("119.00"),
        "line_items": [
            {
                "description": _field("  Widget  x2 "),
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


def _contract(payload: dict | None = None) -> InvoiceExtraction:
    return InvoiceExtraction.model_validate(payload or _base_payload())


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


async def _completed_extraction(
    session: AsyncSession, payload: dict | None = None
) -> ExtractionAttempt:
    doc = await _make_document(session)
    contract = _contract(payload)

    async def produce(_doc: Document) -> InvoiceExtraction:
        return contract

    return await ExtractionService(session).start(
        doc.document_id, produce=produce, provider_name="fake"
    )


async def _failed_extraction(session: AsyncSession) -> ExtractionAttempt:
    doc = await _make_document(session)

    async def boom(_doc: Document) -> InvoiceExtraction:
        raise RuntimeError("extraction provider fell over")

    return await ExtractionService(session).start(
        doc.document_id, produce=boom, provider_name="fake"
    )


# --- happy path ------------------------------------------------------- --


async def test_start_normalizes_every_field_and_line_item(
    db_session: AsyncSession,
) -> None:
    extraction = await _completed_extraction(db_session)
    extraction_id = extraction.extraction_id

    attempt = await NormalizationService(db_session).start(extraction_id)

    assert attempt.status is NormalizationStatus.COMPLETED
    assert attempt.attempt_number == 1
    assert attempt.extraction_id == extraction_id
    assert attempt.started_at is not None and attempt.completed_at is not None
    assert attempt.failure_code is None and attempt.failure_message is None
    assert list(attempt.errors) == []

    # canonical scalar values
    assert attempt.invoice_number == "INV-77"
    assert attempt.invoice_date == "2026-03-03"  # "3 March 2026" -> ISO
    assert attempt.due_date is None
    assert attempt.vendor_name == "Acme GmbH"  # whitespace collapsed
    assert attempt.vendor_tax_id == "DE 123 456 789"  # separators preserved
    assert attempt.currency == "EUR"  # trimmed + upper-cased
    assert attempt.subtotal == Decimal("100.00")
    assert attempt.total_amount == Decimal("119.00")  # sign/scale preserved

    # line items, in source order
    assert [li.position for li in attempt.line_items] == [0, 1]
    assert attempt.line_items[0].description == "Widget x2"
    assert attempt.line_items[0].quantity == Decimal("2")
    assert attempt.line_items[0].unit_price == Decimal("10.00")
    assert attempt.line_items[1].description == "Gadget"

    normalization_id = attempt.normalization_id
    db_session.expire_all()
    reloaded = await NormalizationRepository(db_session).get(normalization_id)
    assert reloaded.status is NormalizationStatus.COMPLETED
    assert [li.position for li in reloaded.line_items] == [0, 1]


# --- field errors are data, not technical failures --------------------


async def test_field_errors_do_not_fail_the_attempt(db_session: AsyncSession) -> None:
    payload = _base_payload()
    payload["invoice_date"] = _field("31/02/2026")  # impossible calendar date
    payload["currency"] = _field("ZZZ")  # well-formed, not on the ISO list
    payload["vendor_name"] = _field("x" * 300)  # over the 256-char cap
    payload["line_items"][0]["description"] = _field("d" * 600)  # over 512

    attempt = await NormalizationService(db_session).start(
        (await _completed_extraction(db_session, payload)).extraction_id
    )

    assert attempt.status is NormalizationStatus.COMPLETED
    assert attempt.completed_at is not None
    assert attempt.failure_code is None

    errors = {err.field_path: err for err in attempt.errors}
    assert set(errors) == {
        "invoice_date",
        "currency",
        "vendor_name",
        "line_items.0.description",
    }
    assert errors["invoice_date"].code is NormalizationErrorCode.INVALID_DATE
    assert errors["invoice_date"].raw_value == "31/02/2026"
    assert errors["currency"].code is NormalizationErrorCode.UNKNOWN_CURRENCY
    assert errors["vendor_name"].code is NormalizationErrorCode.TEXT_TOO_LONG
    assert len(errors["vendor_name"].raw_value) == 300
    assert errors["line_items.0.description"].code is NormalizationErrorCode.TEXT_TOO_LONG

    # errored fields are null; their untouched siblings still normalized
    assert attempt.invoice_date is None
    assert attempt.currency is None
    assert attempt.vendor_name is None
    assert attempt.line_items[0].description is None
    assert attempt.line_items[0].quantity == Decimal("2")
    assert attempt.invoice_number == "INV-77"
    assert attempt.line_items[1].description == "Gadget"


async def test_null_and_empty_values_normalize_to_null_without_error(
    db_session: AsyncSession,
) -> None:
    payload = _base_payload()
    payload["due_date"] = _field(None)
    payload["vendor_tax_id"] = _field("   ")  # whitespace only
    payload["customer_name"] = _field("")

    attempt = await NormalizationService(db_session).start(
        (await _completed_extraction(db_session, payload)).extraction_id
    )

    assert attempt.status is NormalizationStatus.COMPLETED
    assert list(attempt.errors) == []
    assert attempt.due_date is None
    assert attempt.vendor_tax_id is None
    assert attempt.customer_name is None


# --- not found / conflicts ------------------------------------------------


async def test_start_on_unknown_extraction_raises_not_found(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(NotFoundError) as excinfo:
        await NormalizationService(db_session).start(uuid.uuid4())
    assert excinfo.value.code == "EXTRACTION_NOT_FOUND"


async def test_start_rejected_when_source_extraction_did_not_complete(
    db_session: AsyncSession,
) -> None:
    failed = await _failed_extraction(db_session)
    assert failed.status is ExtractionStatus.FAILED

    with pytest.raises(ConflictError) as excinfo:
        await NormalizationService(db_session).start(failed.extraction_id)
    assert excinfo.value.code == "EXTRACTION_NOT_COMPLETED"

    assert (
        await NormalizationRepository(db_session).list_for_extraction(
            failed.extraction_id
        )
        == []
    )


async def test_start_rejected_when_extraction_already_normalized(
    db_session: AsyncSession,
) -> None:
    extraction_id = (await _completed_extraction(db_session)).extraction_id
    svc = NormalizationService(db_session)

    first = await svc.start(extraction_id)
    assert first.status is NormalizationStatus.COMPLETED

    with pytest.raises(ConflictError) as excinfo:
        await svc.start(extraction_id)
    assert excinfo.value.code == "EXTRACTION_ALREADY_NORMALIZED"


async def test_an_active_attempt_blocks_a_new_start(db_session: AsyncSession) -> None:
    extraction = await _completed_extraction(db_session)
    db_session.add(
        NormalizationAttempt(
            extraction_id=extraction.extraction_id,
            attempt_number=1,
            status=NormalizationStatus.PROCESSING,
        )
    )
    await db_session.commit()

    with pytest.raises(ConflictError) as excinfo:
        await NormalizationService(db_session).start(extraction.extraction_id)
    assert excinfo.value.code == "NORMALIZATION_IN_PROGRESS"


async def test_retry_requires_a_technically_failed_attempt(
    db_session: AsyncSession,
) -> None:
    extraction_id = (await _completed_extraction(db_session)).extraction_id
    svc = NormalizationService(db_session)

    with pytest.raises(ConflictError) as excinfo:
        await svc.retry(extraction_id)
    assert excinfo.value.code == "NORMALIZATION_NOT_FAILED"

    await svc.start(extraction_id)  # -> COMPLETED
    with pytest.raises(ConflictError) as excinfo:
        await svc.retry(extraction_id)
    assert excinfo.value.code == "NORMALIZATION_NOT_FAILED"


async def test_two_concurrent_starts_only_one_wins(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup:
        extraction_id = (await _completed_extraction(setup)).extraction_id

    async def run() -> NormalizationAttempt:
        async with session_factory() as session:
            return await NormalizationService(session).start(extraction_id)

    results = await asyncio.gather(run(), run(), return_exceptions=True)

    winners = [r for r in results if isinstance(r, NormalizationAttempt)]
    conflicts = [r for r in results if isinstance(r, ConflictError)]
    assert len(winners) == 1 and len(conflicts) == 1
    assert winners[0].status is NormalizationStatus.COMPLETED
    assert conflicts[0].code in {
        "NORMALIZATION_IN_PROGRESS",
        "EXTRACTION_ALREADY_NORMALIZED",
    }

    async with session_factory() as check:
        history = await NormalizationRepository(check).list_for_extraction(extraction_id)
    assert [a.attempt_number for a in history] == [1]


# --- technical failure handling -------------------------------------- ----


async def test_engine_exception_marks_failed_with_a_safe_message(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    extraction_id = (await _completed_extraction(db_session)).extraction_id

    def boom(_contract: InvoiceExtraction):
        raise RuntimeError("engine blew up host=10.0.0.9 key=sk-secret")

    monkeypatch.setattr(_ENGINE_PATH, boom)

    attempt = await NormalizationService(db_session).start(extraction_id)

    assert attempt.status is NormalizationStatus.FAILED
    assert attempt.failure_code == "NORMALIZATION_FAILED"
    assert attempt.failure_message == _GENERIC_FAILURE
    assert "sk-secret" not in (attempt.failure_message or "")
    assert attempt.completed_at is not None
    assert list(attempt.line_items) == [] and list(attempt.errors) == []
    assert attempt.invoice_number is None and attempt.total_amount is None


async def test_persist_failure_rolls_back_and_stores_no_partial_result(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    extraction_id = (await _completed_extraction(db_session)).extraction_id

    def explode(self, attempt, normalized) -> None:  # noqa: ANN001
        # Half-write some children, then fail - the service must roll it back.
        attempt.errors = [
            NormalizationFieldError(
                field_path="currency",
                raw_value="ZZZ",
                code=NormalizationErrorCode.UNKNOWN_CURRENCY,
                message="bad",
            )
        ]
        raise RuntimeError("database write failed mid-flush")

    monkeypatch.setattr(NormalizationRepository, "apply_result", explode)

    attempt = await NormalizationService(db_session).start(extraction_id)

    assert attempt.status is NormalizationStatus.FAILED
    assert attempt.failure_code == "NORMALIZATION_FAILED"
    assert list(attempt.line_items) == [] and list(attempt.errors) == []
    assert attempt.invoice_number is None

    db_session.expire_all()
    line_item_count = await db_session.scalar(
        select(func.count()).select_from(NormalizationLineItem)
    )
    error_count = await db_session.scalar(
        select(func.count()).select_from(NormalizationFieldError)
    )
    assert line_item_count == 0 and error_count == 0


async def test_retry_after_technical_failure_creates_new_attempt_and_completes(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    extraction_id = (await _completed_extraction(db_session)).extraction_id
    svc = NormalizationService(db_session)

    def boom(_contract: InvoiceExtraction):
        raise RuntimeError("transient engine crash")

    monkeypatch.setattr(_ENGINE_PATH, boom)
    first = await svc.start(extraction_id)
    assert first.status is NormalizationStatus.FAILED

    monkeypatch.undo()
    second = await svc.retry(extraction_id)
    assert second.status is NormalizationStatus.COMPLETED
    assert second.attempt_number == 2

    history = await NormalizationRepository(db_session).list_for_extraction(extraction_id)
    assert [(a.attempt_number, a.status) for a in history] == [
        (1, NormalizationStatus.FAILED),
        (2, NormalizationStatus.COMPLETED),
    ]
    # The failed attempt was not mutated by the retry.
    assert history[0].failure_code == "NORMALIZATION_FAILED"
    assert history[0].invoice_number is None
    assert list(history[0].line_items) == []
    assert history[1].invoice_number == "INV-77"


# --- source immutability -------------------------------------------------


async def test_source_extraction_and_document_are_untouched(
    db_session: AsyncSession,
) -> None:
    payload = _base_payload()
    payload["invoice_date"] = _field("31/02/2026")  # even an unnormalizable value
    extraction = await _completed_extraction(db_session, payload)
    extraction_id = extraction.extraction_id
    document_id = extraction.document_id

    document = await db_session.get(Document, document_id)
    file_fingerprint = (
        document.file_location,
        document.file_hash,
        document.file_size_bytes,
        document.page_count,
    )

    await NormalizationService(db_session).start(extraction_id)

    db_session.expire_all()
    reloaded = await ExtractionRepository(db_session).get(extraction_id)
    assert reloaded.status is ExtractionStatus.COMPLETED
    assert reloaded.invoice_date_value == "31/02/2026"  # raw text preserved verbatim
    assert reloaded.currency_value == "EUR"
    assert reloaded.total_amount_value == Decimal("119.00")
    assert [li.position for li in reloaded.line_items] == [0, 1]

    # Normalization never touches storage or the document row.
    reloaded_doc = await db_session.get(Document, document_id)
    assert reloaded_doc.status is DocumentStatus.COMPLETED
    assert (
        reloaded_doc.file_location,
        reloaded_doc.file_hash,
        reloaded_doc.file_size_bytes,
        reloaded_doc.page_count,
    ) == file_fingerprint


async def test_normalization_makes_no_http_call(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    extraction_id = (await _completed_extraction(db_session)).extraction_id

    def forbid(*_args, **_kwargs):
        raise AssertionError("normalization attempted an outbound HTTP request")

    monkeypatch.setattr(httpx.Client, "send", forbid)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbid)

    attempt = await NormalizationService(db_session).start(extraction_id)
    assert attempt.status is NormalizationStatus.COMPLETED


# --- engine unit-ish (no database) -------------------------------------


def test_engine_is_total_over_the_contract_and_collects_field_errors() -> None:
    payload = _base_payload()
    payload["invoice_date"] = _field("2026-13-40")
    payload["currency"] = _field("ZZZ")

    normalized = normalize_extraction(_contract(payload))

    for name in NORMALIZED_SCALAR_FIELD_NAMES:
        assert hasattr(normalized, name)
    assert normalized.invoice_number == "INV-77"
    assert normalized.invoice_date is None
    assert normalized.currency is None
    assert {err.field_path for err in normalized.errors} == {"invoice_date", "currency"}
    assert len(normalized.line_items) == 2


def test_engine_preserves_decimal_sign_and_scale() -> None:
    payload = _base_payload()
    payload["subtotal"] = _field("-100.500")
    payload["line_items"][0]["quantity"] = _field("2.0")

    normalized = normalize_extraction(_contract(payload))

    assert normalized.subtotal == Decimal("-100.500")
    assert str(normalized.subtotal) == "-100.500"
    assert str(normalized.line_items[0].quantity) == "2.0"


# --- lifecycle guards -------------------------------------------------


def test_lifecycle_attempt_transition_table() -> None:
    lifecycle.ensure_attempt_transition(
        NormalizationStatus.PROCESSING, NormalizationStatus.COMPLETED
    )
    lifecycle.ensure_attempt_transition(
        NormalizationStatus.PROCESSING, NormalizationStatus.FAILED
    )
    for bad in (
        (NormalizationStatus.PROCESSING, NormalizationStatus.PROCESSING),
        (NormalizationStatus.COMPLETED, NormalizationStatus.FAILED),
        (NormalizationStatus.FAILED, NormalizationStatus.COMPLETED),
        (NormalizationStatus.COMPLETED, NormalizationStatus.PROCESSING),
    ):
        with pytest.raises(ValueError):
            lifecycle.ensure_attempt_transition(*bad)


@pytest.mark.parametrize("status", [ExtractionStatus.PROCESSING, ExtractionStatus.FAILED])
def test_lifecycle_requires_a_completed_source_extraction(
    status: ExtractionStatus,
) -> None:
    for action in ("start", "retry"):
        with pytest.raises(ConflictError) as excinfo:
            lifecycle.ensure_extraction_can_normalize(status, None, action=action)
        assert excinfo.value.code == "EXTRACTION_NOT_COMPLETED"


@pytest.mark.parametrize(
    ("latest", "action", "code"),
    [
        (NormalizationStatus.PROCESSING, "start", "NORMALIZATION_IN_PROGRESS"),
        (NormalizationStatus.COMPLETED, "start", "EXTRACTION_ALREADY_NORMALIZED"),
        (NormalizationStatus.FAILED, "start", "NORMALIZATION_FAILED"),
        (None, "retry", "NORMALIZATION_NOT_FAILED"),
        (NormalizationStatus.PROCESSING, "retry", "NORMALIZATION_IN_PROGRESS"),
        (NormalizationStatus.COMPLETED, "retry", "NORMALIZATION_NOT_FAILED"),
    ],
)
def test_lifecycle_gating_by_latest_attempt(
    latest: NormalizationStatus | None, action: str, code: str
) -> None:
    with pytest.raises(ConflictError) as excinfo:
        lifecycle.ensure_extraction_can_normalize(
            ExtractionStatus.COMPLETED, latest, action=action
        )
    assert excinfo.value.code == code


def test_lifecycle_allows_the_valid_starts() -> None:
    lifecycle.ensure_extraction_can_normalize(
        ExtractionStatus.COMPLETED, None, action="start"
    )
    lifecycle.ensure_extraction_can_normalize(
        ExtractionStatus.COMPLETED, NormalizationStatus.FAILED, action="retry"
    )
