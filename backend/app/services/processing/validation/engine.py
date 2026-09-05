"""Stage 5 validation engine (step 9).

Given one ``COMPLETED`` :class:`~app.models.normalization.NormalizationAttempt`
(loaded with its line items and field errors) and the validation attempt's own
``started_at``, :func:`evaluate`:

* rebuilds the Stage 4 :class:`~app.schemas.normalization.NormalizedInvoice`
  from the stored attempt (the flat->nested bridge in
  :mod:`app.schemas.normalization_persistence`);
* reads the five ``<critical_field>_confidence`` columns from the source
  ``invoice_extractions`` row - and no other extraction value (spec §2.6);
* builds the §2.5 duplicate-candidate set (the latest ``COMPLETED``
  normalization of the latest ``COMPLETED`` extraction of every *other*
  document, as of ``started_at``);
* runs every rule
  (:func:`app.services.processing.validation.rules.run_rules`) and assembles a
  schema-valid :class:`~app.schemas.validation.InvoiceValidation`.

The engine **reads** Stage 2-4 rows and **writes nothing**. It makes no AI call
and no external-network call; its only I/O is SQL through the caller's session,
and its only non-deterministic input is ``started_at`` (spec §2.8). Persisting
the result, the lifecycle, and retries are the step 10-11 service.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.extraction import ExtractionAttempt, ExtractionStatus
from app.models.normalization import NormalizationAttempt, NormalizationStatus
from app.schemas.normalization_persistence import normalized_invoice_from_attempt
from app.schemas.validation import InvoiceValidation
from app.services.processing.validation import policy
from app.services.processing.validation.rules import (
    DuplicateCandidate,
    RuleContext,
    run_rules,
)

__all__ = [
    "evaluate",
    "load_attempt",
    "load_confidence",
    "load_duplicate_candidates",
    "CRITICAL_CONFIDENCE_COLUMNS",
]

# critical field name -> the confidence column on ``invoice_extractions``.
CRITICAL_CONFIDENCE_COLUMNS: dict[str, str] = {
    name: f"{name}_confidence" for name in policy.CRITICAL_FIELDS
}

# Fail fast at import if the extraction model ever loses a confidence column.
for _column in CRITICAL_CONFIDENCE_COLUMNS.values():
    if not hasattr(ExtractionAttempt, _column):
        raise RuntimeError(
            f"invoice_extractions has no column {_column!r}; the Stage 5 "
            "confidence read is out of step with the extraction model"
        )


def _as_utc_date(moment: datetime) -> date:
    """``started_at`` as a UTC calendar date (spec §2.8)."""
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(timezone.utc).date()


async def load_confidence(
    session: AsyncSession, extraction_id: uuid.UUID
) -> dict[str, Decimal | None]:
    """The per-critical-field extraction confidence, keyed by field name.

    Each value is a ``Decimal`` in ``[0, 1]`` or ``None`` (the provider gave no
    calibrated confidence). Only the five critical columns are read.
    """
    columns = [
        getattr(ExtractionAttempt, CRITICAL_CONFIDENCE_COLUMNS[name])
        for name in policy.CRITICAL_FIELDS
    ]
    row = (
        await session.execute(
            select(*columns).where(
                ExtractionAttempt.extraction_id == extraction_id
            )
        )
    ).one()
    return dict(zip(policy.CRITICAL_FIELDS, row))


async def load_duplicate_candidates(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    as_of: datetime,
) -> list[DuplicateCandidate]:
    """The §2.5 candidate set: one row per *other* document, or none.

    Per other document: its highest ``COMPLETED`` extraction ``attempt_number``,
    then that extraction's highest ``COMPLETED`` normalization ``attempt_number``
    whose ``completed_at <= as_of``. A document whose latest completed extraction
    has no qualifying completed normalization contributes nothing - there is no
    fall-back to an older extraction.
    """
    latest_extraction = (
        select(
            ExtractionAttempt.document_id.label("document_id"),
            func.max(ExtractionAttempt.attempt_number).label("attempt_number"),
        )
        .where(
            ExtractionAttempt.status == ExtractionStatus.COMPLETED,
            ExtractionAttempt.completed_at <= as_of,
            ExtractionAttempt.document_id != document_id,
        )
        .group_by(ExtractionAttempt.document_id)
        .subquery()
    )
    latest_completed_extractions = (
        select(
            ExtractionAttempt.extraction_id.label("extraction_id"),
            ExtractionAttempt.document_id.label("document_id"),
        )
        .join(
            latest_extraction,
            (ExtractionAttempt.document_id == latest_extraction.c.document_id)
            & (
                ExtractionAttempt.attempt_number
                == latest_extraction.c.attempt_number
            ),
        )
        .where(ExtractionAttempt.status == ExtractionStatus.COMPLETED)
        .subquery()
    )
    latest_normalization = (
        select(
            NormalizationAttempt.extraction_id.label("extraction_id"),
            func.max(NormalizationAttempt.attempt_number).label("attempt_number"),
        )
        .where(
            NormalizationAttempt.status == NormalizationStatus.COMPLETED,
            NormalizationAttempt.completed_at <= as_of,
            NormalizationAttempt.extraction_id.in_(
                select(latest_completed_extractions.c.extraction_id)
            ),
        )
        .group_by(NormalizationAttempt.extraction_id)
        .subquery()
    )
    stmt = (
        select(
            latest_completed_extractions.c.document_id,
            NormalizationAttempt.normalization_id,
            NormalizationAttempt.vendor_tax_id,
            NormalizationAttempt.vendor_name,
            NormalizationAttempt.invoice_number,
            NormalizationAttempt.currency,
            NormalizationAttempt.total_amount,
        )
        .select_from(NormalizationAttempt)
        .join(
            latest_normalization,
            (
                NormalizationAttempt.extraction_id
                == latest_normalization.c.extraction_id
            )
            & (
                NormalizationAttempt.attempt_number
                == latest_normalization.c.attempt_number
            ),
        )
        .join(
            latest_completed_extractions,
            latest_completed_extractions.c.extraction_id
            == NormalizationAttempt.extraction_id,
        )
        .where(NormalizationAttempt.status == NormalizationStatus.COMPLETED)
    )
    rows = (await session.execute(stmt)).all()
    return [
        DuplicateCandidate(
            document_id=row.document_id,
            normalization_id=row.normalization_id,
            vendor_tax_id=row.vendor_tax_id,
            vendor_name=row.vendor_name,
            invoice_number=row.invoice_number,
            currency=row.currency,
            total_amount=row.total_amount,
        )
        for row in rows
    ]


async def load_attempt(
    session: AsyncSession, normalization_id: uuid.UUID
) -> NormalizationAttempt:
    """The normalization attempt with its line items and field errors loaded."""
    return (
        await session.execute(
            select(NormalizationAttempt)
            .where(NormalizationAttempt.normalization_id == normalization_id)
            .options(
                selectinload(NormalizationAttempt.line_items),
                selectinload(NormalizationAttempt.errors),
            )
        )
    ).scalar_one()


async def evaluate(
    session: AsyncSession,
    normalization_id: uuid.UUID,
    *,
    started_at: datetime,
) -> InvoiceValidation:
    """Run Stage 5 over one completed normalization attempt.

    Loads the attempt (with its errors), the source extraction's confidence
    columns, and the §2.5 duplicate candidates, then returns a re-validated
    :class:`InvoiceValidation`. Reads only; writes nothing. The caller
    (step 10-11 service) is responsible for the attempt being ``COMPLETED`` and
    for persisting the result.
    """
    attempt = await load_attempt(session, normalization_id)
    document_id = await session.scalar(
        select(ExtractionAttempt.document_id).where(
            ExtractionAttempt.extraction_id == attempt.extraction_id
        )
    )
    context = RuleContext(
        invoice=normalized_invoice_from_attempt(attempt),
        run_date=_as_utc_date(started_at),
        confidence=await load_confidence(session, attempt.extraction_id),
        duplicate_candidates=await load_duplicate_candidates(
            session, document_id=document_id, as_of=started_at
        ),
    )
    return InvoiceValidation.from_findings(run_rules(context))
