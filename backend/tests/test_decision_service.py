"""Tests for the decision repository/service, and lifecycle wiring
(Stage 6, package 4).

These run against the real PostgreSQL test database (via the ``db_session``
fixture). A completed source validation is built directly from ORM rows (no
extraction/normalization/validation service involved - Stage 6 only reads
their output); a technical failure is simulated by monkeypatching the
service's imported ``decide`` or the repository's ``apply_result``.

Every test captures the plain ids it needs (``validation_id``,
``document_id``, ...) right after building the fixture, rather than holding
onto the ORM object across a service call - a rollback inside the service
(``_mark_failed``) expires every object in the session, and re-touching an
expired attribute synchronously outside an ``await`` raises
``MissingGreenlet`` under the async engine.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError, NotFoundError
from app.models import (
    Document,
    DocumentStatus,
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
from app.models.decision import (
    DecisionAttempt,
    DecisionOutcome,
    DecisionReasonRow,
    DecisionStatus,
)
from app.services.processing.decision.repository import DecisionRepository
from app.services.processing.decision.service import _GENERIC_FAILURE, DecisionService

_DECIDE_PATH = "app.services.processing.decision.service.decide"


def _finding(position: int, **kw) -> ValidationFindingRow:
    data = {
        "position": position,
        "rule": ValidationRule.NO_LINE_ITEMS,
        "severity": FindingSeverity.INFO,
        "field_path": None,
        "expected": None,
        "actual": None,
        "message": "The invoice has no line items.",
        "context": {},
    }
    data.update(kw)
    return ValidationFindingRow(**data)


async def _completed_validation(
    session: AsyncSession, findings: list[ValidationFindingRow] | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    """Build a full COMPLETED chain; return ``(validation_id, document_id)``."""
    doc = Document(
        original_filename="invoice.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="a" * 64,
        file_size_bytes=1234,
        page_count=1,
        # A real completed extraction always leaves the document COMPLETED
        # (ExtractionService._complete) - built directly here since this
        # helper bypasses ExtractionService, but the precondition must match.
        status=DocumentStatus.COMPLETED,
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
    if findings is not None:
        validation.findings = findings
    session.add(validation)
    await session.flush()
    await session.commit()
    return validation.validation_id, doc.document_id


async def _failed_validation(session: AsyncSession) -> uuid.UUID:
    """Build a chain whose validation attempt FAILED technically; return its id."""
    doc = Document(
        original_filename="invoice.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="b" * 64,
        file_size_bytes=1234,
        page_count=1,
        status=DocumentStatus.COMPLETED,
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
    attempt = ValidationAttempt(
        normalization_id=normalization.normalization_id,
        attempt_number=1,
        status=ValidationStatus.FAILED,
        completed_at=datetime.now(timezone.utc),
        failure_code="VALIDATION_FAILED",
        failure_message="boom",
    )
    session.add(attempt)
    await session.flush()
    await session.commit()
    return attempt.validation_id


async def _document_status(session: AsyncSession, document_id: uuid.UUID) -> DocumentStatus:
    return await session.scalar(
        select(Document.status).where(Document.document_id == document_id)
    )


# --- happy path ----------------------------------------------------------


async def test_start_decides_a_clean_invoice_as_accepted(db_session: AsyncSession) -> None:
    validation_id, document_id = await _completed_validation(db_session, findings=[])

    attempt = await DecisionService(db_session).start(validation_id)

    assert attempt.status is DecisionStatus.COMPLETED
    assert attempt.outcome is DecisionOutcome.ACCEPTED
    assert attempt.attempt_number == 1
    assert attempt.reasons == []
    assert attempt.failure_code is None and attempt.failure_message is None
    assert await _document_status(db_session, document_id) is DocumentStatus.COMPLETED


async def test_needs_review_finding_moves_the_document_to_needs_review(
    db_session: AsyncSession,
) -> None:
    finding = _finding(0, rule=ValidationRule.HIGH_VALUE_INVOICE, severity=FindingSeverity.INFO)
    validation_id, document_id = await _completed_validation(db_session, findings=[finding])

    attempt = await DecisionService(db_session).start(validation_id)

    assert attempt.status is DecisionStatus.COMPLETED
    assert attempt.outcome is DecisionOutcome.NEEDS_REVIEW
    assert len(attempt.reasons) == 1
    assert attempt.reasons[0].source_finding_id == finding.validation_finding_id
    assert attempt.reasons[0].triggers_review is True
    assert await _document_status(db_session, document_id) is DocumentStatus.NEEDS_REVIEW


async def test_only_non_gating_findings_stay_accepted_and_document_stays_completed(
    db_session: AsyncSession,
) -> None:
    finding = _finding(0, rule=ValidationRule.NO_LINE_ITEMS)
    validation_id, document_id = await _completed_validation(db_session, findings=[finding])

    attempt = await DecisionService(db_session).start(validation_id)

    assert attempt.outcome is DecisionOutcome.ACCEPTED
    assert len(attempt.reasons) == 1  # kept, not discarded
    assert attempt.reasons[0].triggers_review is False
    assert await _document_status(db_session, document_id) is DocumentStatus.COMPLETED


# --- not found / conflicts ------------------------------------------------


async def test_start_on_unknown_validation_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError) as excinfo:
        await DecisionService(db_session).start(uuid.uuid4())
    assert excinfo.value.code == "VALIDATION_NOT_FOUND"


@pytest.mark.parametrize("invalid", [0, 1, "false", None])
async def test_invalid_manual_review_input_creates_no_attempt(
    db_session: AsyncSession, invalid: object
) -> None:
    validation_id, _ = await _completed_validation(db_session, findings=[])

    with pytest.raises(TypeError, match="manual_review_requested must be a bool"):
        await DecisionService(db_session).start(
            validation_id, manual_review_requested=invalid  # type: ignore[arg-type]
        )

    assert await DecisionRepository(db_session).list_for_validation(validation_id) == []


async def test_start_rejected_when_source_validation_not_completed(
    db_session: AsyncSession,
) -> None:
    failed_id = await _failed_validation(db_session)

    with pytest.raises(ConflictError) as excinfo:
        await DecisionService(db_session).start(failed_id)
    assert excinfo.value.code == "VALIDATION_NOT_COMPLETED"

    assert await DecisionRepository(db_session).list_for_validation(failed_id) == []


async def test_start_rejected_when_already_decided(db_session: AsyncSession) -> None:
    validation_id, _ = await _completed_validation(db_session, findings=[])
    svc = DecisionService(db_session)

    first = await svc.start(validation_id)
    assert first.status is DecisionStatus.COMPLETED

    with pytest.raises(ConflictError) as excinfo:
        await svc.start(validation_id)
    assert excinfo.value.code == "VALIDATION_ALREADY_DECIDED"


async def test_an_active_attempt_blocks_a_new_start(db_session: AsyncSession) -> None:
    validation_id, _ = await _completed_validation(db_session, findings=[])
    db_session.add(
        DecisionAttempt(
            validation_id=validation_id,
            attempt_number=1,
            status=DecisionStatus.PROCESSING,
            policy_version="1",
        )
    )
    await db_session.commit()

    with pytest.raises(ConflictError) as excinfo:
        await DecisionService(db_session).start(validation_id)
    assert excinfo.value.code == "DECISION_IN_PROGRESS"


async def test_retry_requires_a_technically_failed_attempt(db_session: AsyncSession) -> None:
    validation_id, _ = await _completed_validation(db_session, findings=[])
    svc = DecisionService(db_session)

    with pytest.raises(ConflictError) as excinfo:
        await svc.retry(validation_id)
    assert excinfo.value.code == "DECISION_NOT_FAILED"

    await svc.start(validation_id)  # -> COMPLETED
    with pytest.raises(ConflictError) as excinfo:
        await svc.retry(validation_id)
    assert excinfo.value.code == "DECISION_NOT_FAILED"


async def test_two_concurrent_starts_only_one_wins(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup:
        validation_id, _ = await _completed_validation(setup, findings=[])

    async def run() -> DecisionAttempt:
        async with session_factory() as session:
            return await DecisionService(session).start(validation_id)

    results = await asyncio.gather(run(), run(), return_exceptions=True)

    winners = [r for r in results if isinstance(r, DecisionAttempt)]
    conflicts = [r for r in results if isinstance(r, ConflictError)]
    assert len(winners) == 1 and len(conflicts) == 1
    assert winners[0].status is DecisionStatus.COMPLETED
    assert conflicts[0].code in {"DECISION_IN_PROGRESS", "VALIDATION_ALREADY_DECIDED"}

    async with session_factory() as check:
        history = await DecisionRepository(check).list_for_validation(validation_id)
    assert [a.attempt_number for a in history] == [1]


# --- technical failure handling -------------------------------------------


async def test_engine_exception_marks_failed_with_a_safe_message_and_leaves_document_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    validation_id, document_id = await _completed_validation(db_session, findings=[])

    def boom(*_args, **_kwargs):
        raise RuntimeError("engine blew up host=10.0.0.9 key=sk-secret")

    monkeypatch.setattr(_DECIDE_PATH, boom)

    attempt = await DecisionService(db_session).start(validation_id)

    assert attempt.status is DecisionStatus.FAILED
    assert attempt.failure_code == "DECISION_FAILED"
    assert attempt.failure_message == _GENERIC_FAILURE
    assert "sk-secret" not in (attempt.failure_message or "")
    assert attempt.outcome is None
    assert list(attempt.reasons) == []

    # A technical failure is not a fact about the invoice: the document stays
    # exactly where extraction left it.
    assert await _document_status(db_session, document_id) is DocumentStatus.COMPLETED


async def test_persist_failure_rolls_back_and_stores_no_partial_result(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    finding = _finding(0, rule=ValidationRule.HIGH_VALUE_INVOICE, severity=FindingSeverity.INFO)
    validation_id, document_id = await _completed_validation(db_session, findings=[finding])

    def explode(self, attempt, result, source_finding_ids) -> None:  # noqa: ANN001
        # Half-write a reason, then fail - the service must roll it back.
        attempt.reasons = [
            DecisionReasonRow(
                position=0,
                code="manual_review_requested",
                triggers_review=True,
                source_rule=None,
                source_finding_id=None,
                field_path=None,
                message="x",
            )
        ]
        raise RuntimeError("database write failed mid-flush")

    monkeypatch.setattr(DecisionRepository, "apply_result", explode)

    attempt = await DecisionService(db_session).start(validation_id)

    assert attempt.status is DecisionStatus.FAILED
    assert attempt.failure_code == "DECISION_FAILED"
    assert list(attempt.reasons) == []

    db_session.expire_all()
    reason_count = await db_session.scalar(select(func.count()).select_from(DecisionReasonRow))
    assert reason_count == 0
    # NEEDS_REVIEW must never be written from a fault mid-completion.
    assert await _document_status(db_session, document_id) is DocumentStatus.COMPLETED


async def test_retry_after_technical_failure_creates_new_attempt_and_completes(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    validation_id, _ = await _completed_validation(db_session, findings=[])
    svc = DecisionService(db_session)

    def boom(*_args, **_kwargs):
        raise RuntimeError("transient engine crash")

    monkeypatch.setattr(_DECIDE_PATH, boom)
    first = await svc.start(validation_id)
    assert first.status is DecisionStatus.FAILED

    monkeypatch.undo()
    second = await svc.retry(validation_id)
    assert second.status is DecisionStatus.COMPLETED
    assert second.attempt_number == 2

    history = await DecisionRepository(db_session).list_for_validation(validation_id)
    assert [(a.attempt_number, a.status) for a in history] == [
        (1, DecisionStatus.FAILED),
        (2, DecisionStatus.COMPLETED),
    ]
    # The failed attempt was not mutated by the retry.
    assert history[0].failure_code == "DECISION_FAILED"
    assert list(history[0].reasons) == []
    assert history[1].outcome is DecisionOutcome.ACCEPTED


# --- source immutability --------------------------------------------------


async def test_source_validation_and_earlier_rows_are_untouched(
    db_session: AsyncSession,
) -> None:
    finding = _finding(0, rule=ValidationRule.HIGH_VALUE_INVOICE, severity=FindingSeverity.INFO)
    validation_id, _ = await _completed_validation(db_session, findings=[finding])

    fetched = await db_session.get(ValidationAttempt, validation_id)
    val_updated_at = fetched.updated_at
    normalization_id = fetched.normalization_id
    val_finding_fingerprint = (
        await db_session.execute(
            select(ValidationFindingRow.rule, ValidationFindingRow.message).where(
                ValidationFindingRow.validation_id == validation_id
            )
        )
    ).all()

    await DecisionService(db_session).start(validation_id)

    db_session.expire_all()
    reloaded_validation = await db_session.get(ValidationAttempt, validation_id)
    assert reloaded_validation.updated_at == val_updated_at
    assert reloaded_validation.normalization_id == normalization_id
    reloaded_findings = (
        await db_session.execute(
            select(ValidationFindingRow.rule, ValidationFindingRow.message).where(
                ValidationFindingRow.validation_id == validation_id
            )
        )
    ).all()
    assert reloaded_findings == val_finding_fingerprint


# --- stale source protection (§6.4) ---------------------------------------


async def test_a_superseded_extraction_chain_is_rejected_as_stale(
    db_session: AsyncSession,
) -> None:
    validation_id, document_id = await _completed_validation(db_session, findings=[])

    # Manufacture a second, newer extraction attempt for the same document -
    # not reachable through the real API (extraction only retries a FAILED
    # attempt), but exactly the shape a future re-extraction feature would
    # produce, and exactly what this guard exists to catch.
    newer_extraction = ExtractionAttempt(
        document_id=document_id,
        attempt_number=2,
        status=ExtractionStatus.COMPLETED,
        completed_at=datetime.now(timezone.utc),
        provider_name="fake",
    )
    db_session.add(newer_extraction)
    await db_session.commit()

    with pytest.raises(ConflictError) as excinfo:
        await DecisionService(db_session).start(validation_id)
    assert excinfo.value.code == "STALE_VALIDATION_SOURCE"

    assert await DecisionRepository(db_session).list_for_validation(validation_id) == []


async def test_source_superseded_during_evaluation_cannot_update_document(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    validation_id, document_id = await _completed_validation(db_session, findings=[])
    real_evaluate = DecisionService._evaluate

    async def supersede_after_initial_guard(self, source_validation_id, **kwargs):  # noqa: ANN001
        result = await real_evaluate(self, source_validation_id, **kwargs)
        self._session.add(
            ExtractionAttempt(
                document_id=document_id,
                attempt_number=2,
                status=ExtractionStatus.COMPLETED,
                completed_at=datetime.now(timezone.utc),
                provider_name="fake",
            )
        )
        await self._session.commit()
        return result

    monkeypatch.setattr(DecisionService, "_evaluate", supersede_after_initial_guard)

    attempt = await DecisionService(db_session).start(validation_id)

    assert attempt.status is DecisionStatus.FAILED
    assert attempt.failure_code == "DECISION_FAILED"
    assert attempt.outcome is None
    assert list(attempt.reasons) == []
    assert await _document_status(db_session, document_id) is DocumentStatus.COMPLETED
