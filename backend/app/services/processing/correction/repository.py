"""Data access for correction attempts and their field diffs (Stage 8).

``CorrectionRepository`` is the only place that reads or writes the
``invoice_corrections`` / ``invoice_correction_fields`` tables. It owns no
transaction boundary: it stages objects on the session and runs queries; the
:class:`~app.services.processing.correction.service.CorrectionService` decides
when to flush and commit.

``persist_projection`` writes the merged ``base ⊕ corrections`` result as a
*new* ``invoice_normalizations`` attempt (``status = COMPLETED``,
``source_correction_id`` set), reusing the Stage 4
:mod:`app.schemas.normalization_persistence` flatteners so the flat column
layout is derived in one place. It never invokes the Stage 4 engine and never
writes back to any Stage 2-7 row.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.correction import CorrectionAttempt, CorrectionFieldRow
from app.models.decision import DecisionOutcome
from app.models.document import DocumentStatus
from app.models.normalization import (
    NormalizationAttempt,
    NormalizationFieldError,
    NormalizationLineItem,
    NormalizationStatus,
)
from app.schemas.correction import CorrectionStatus, InvoiceCorrection
from app.schemas.correction_persistence import correction_field_rows
from app.schemas.normalization import NormalizedInvoice
from app.schemas.normalization_persistence import (
    error_rows,
    line_item_rows,
    scalar_columns,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CorrectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads ------------------------------------------------------------

    @staticmethod
    def _with_fields(stmt):
        return stmt.options(selectinload(CorrectionAttempt.fields))

    async def get(self, correction_id: uuid.UUID) -> CorrectionAttempt | None:
        result = await self._session.execute(
            self._with_fields(
                select(CorrectionAttempt).where(
                    CorrectionAttempt.correction_id == correction_id
                )
            )
        )
        return result.scalar_one_or_none()

    async def get_for_document(
        self, document_id: uuid.UUID, correction_id: uuid.UUID
    ) -> CorrectionAttempt | None:
        result = await self._session.execute(
            self._with_fields(
                select(CorrectionAttempt).where(
                    CorrectionAttempt.correction_id == correction_id,
                    CorrectionAttempt.document_id == document_id,
                )
            )
        )
        return result.scalar_one_or_none()

    async def list_for_document(
        self, document_id: uuid.UUID
    ) -> Sequence[CorrectionAttempt]:
        """Full attempt history for a document, oldest attempt first."""
        result = await self._session.execute(
            self._with_fields(
                select(CorrectionAttempt)
                .where(CorrectionAttempt.document_id == document_id)
                .order_by(CorrectionAttempt.attempt_number)
            )
        )
        return result.scalars().all()

    async def latest_for_document(
        self, document_id: uuid.UUID
    ) -> CorrectionAttempt | None:
        result = await self._session.execute(
            self._with_fields(
                select(CorrectionAttempt)
                .where(CorrectionAttempt.document_id == document_id)
                .order_by(CorrectionAttempt.attempt_number.desc())
                .limit(1)
            )
        )
        return result.scalar_one_or_none()

    async def active_for_document(
        self, document_id: uuid.UUID
    ) -> CorrectionAttempt | None:
        result = await self._session.execute(
            select(CorrectionAttempt).where(
                CorrectionAttempt.document_id == document_id,
                CorrectionAttempt.status == CorrectionStatus.PROCESSING,
            )
        )
        return result.scalar_one_or_none()

    async def next_attempt_number(self, document_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(
                func.coalesce(func.max(CorrectionAttempt.attempt_number), 0) + 1
            ).where(CorrectionAttempt.document_id == document_id)
        )
        return int(result.scalar_one())

    # --- writes -------------------------------------------------------- --

    def add_attempt(
        self,
        *,
        document_id: uuid.UUID,
        attempt_number: int,
        source_normalization_id: uuid.UUID,
        source_decision_id: uuid.UUID,
        correction: InvoiceCorrection,
        submitted_payload: dict[str, Any],
        policy_version: str,
        origin_document_status: DocumentStatus,
        submitted_at: datetime,
    ) -> CorrectionAttempt:
        """Create a ``PROCESSING`` attempt + its field rows and stage it."""
        attempt = CorrectionAttempt(
            document_id=document_id,
            attempt_number=attempt_number,
            source_normalization_id=source_normalization_id,
            source_decision_id=source_decision_id,
            status=CorrectionStatus.PROCESSING,
            reviewer_name=correction.reviewer_name,
            note=correction.note,
            policy_version=policy_version,
            origin_document_status=origin_document_status,
            submitted_payload=submitted_payload,
            submitted_at=submitted_at,
            fields=[
                CorrectionFieldRow(**row)
                for row in correction_field_rows(correction.entries)
            ],
        )
        self._session.add(attempt)
        return attempt

    def persist_projection(
        self,
        *,
        extraction_id: uuid.UUID,
        attempt_number: int,
        correction_id: uuid.UUID,
        merged: NormalizedInvoice,
    ) -> NormalizationAttempt:
        """Write the merged projection as a new COMPLETED normalization attempt."""
        now = _utcnow()
        attempt = NormalizationAttempt(
            extraction_id=extraction_id,
            attempt_number=attempt_number,
            source_correction_id=correction_id,
            status=NormalizationStatus.COMPLETED,
            started_at=now,
            completed_at=now,
            **scalar_columns(merged),
        )
        attempt.line_items = [
            NormalizationLineItem(**row) for row in line_item_rows(merged)
        ]
        attempt.errors = [
            NormalizationFieldError(**row) for row in error_rows(merged)
        ]
        self._session.add(attempt)
        return attempt

    def record_partial(
        self,
        attempt: CorrectionAttempt,
        *,
        resulting_normalization_id: uuid.UUID | None = None,
        resulting_validation_id: uuid.UUID | None = None,
    ) -> None:
        """Stamp the resulting-chain ids a still-running attempt has produced."""
        if resulting_normalization_id is not None:
            attempt.resulting_normalization_id = resulting_normalization_id
        if resulting_validation_id is not None:
            attempt.resulting_validation_id = resulting_validation_id

    def finalize(
        self,
        attempt: CorrectionAttempt,
        *,
        resulting_normalization_id: uuid.UUID,
        resulting_validation_id: uuid.UUID,
        resulting_decision_id: uuid.UUID,
        resulting_outcome: DecisionOutcome,
        resulting_document_status: DocumentStatus,
    ) -> None:
        attempt.status = CorrectionStatus.COMPLETED
        attempt.completed_at = _utcnow()
        attempt.resulting_normalization_id = resulting_normalization_id
        attempt.resulting_validation_id = resulting_validation_id
        attempt.resulting_decision_id = resulting_decision_id
        attempt.resulting_outcome = resulting_outcome
        attempt.resulting_document_status = resulting_document_status

    def mark_failed(
        self, attempt: CorrectionAttempt, *, code: str, message: str
    ) -> None:
        attempt.status = CorrectionStatus.FAILED
        attempt.completed_at = _utcnow()
        attempt.failure_code = code
        attempt.failure_message = message
        # A FAILED row keeps whatever partial resulting_normalization_id /
        # resulting_validation_id it recorded, but never a business outcome.
        attempt.resulting_decision_id = None
        attempt.resulting_outcome = None
        attempt.resulting_document_status = None


__all__ = ["CorrectionRepository"]
