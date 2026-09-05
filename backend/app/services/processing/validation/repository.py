"""Data access for validation attempts and their findings (Stage 5, step 11).

``ValidationRepository`` is the only place that reads or writes the
``invoice_validations`` / ``invoice_validation_findings`` tables. It owns no
transaction boundary: it stages objects on the session and runs queries, and
:class:`~app.services.processing.validation.service.ValidationService` decides
when to flush and commit.

Writing a validated result goes through
:mod:`app.schemas.validation_persistence`, so the flat column layout is only
ever derived from a validated :class:`InvoiceValidation` contract in one place.
The source ``invoice_normalizations`` row - and every Stage 2-4 row it
transitively derives from - is never written back to.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.validation import (
    ValidationAttempt,
    ValidationFindingRow,
    ValidationStatus,
)
from app.schemas.validation import InvoiceValidation
from app.schemas.validation_persistence import finding_rows


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ValidationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads --------------------------------------------------------- --

    @staticmethod
    def _with_children(stmt):
        return stmt.options(selectinload(ValidationAttempt.findings))

    async def get(self, validation_id: uuid.UUID) -> ValidationAttempt | None:
        """One attempt by id, with its findings loaded."""
        result = await self._session.execute(
            self._with_children(
                select(ValidationAttempt).where(
                    ValidationAttempt.validation_id == validation_id
                )
            )
        )
        return result.scalar_one_or_none()

    async def get_for_normalization(
        self, normalization_id: uuid.UUID, validation_id: uuid.UUID
    ) -> ValidationAttempt | None:
        """One attempt by id, but only if it derives from ``normalization_id``."""
        result = await self._session.execute(
            self._with_children(
                select(ValidationAttempt).where(
                    ValidationAttempt.validation_id == validation_id,
                    ValidationAttempt.normalization_id == normalization_id,
                )
            )
        )
        return result.scalar_one_or_none()

    async def list_for_normalization(
        self, normalization_id: uuid.UUID
    ) -> Sequence[ValidationAttempt]:
        """Full attempt history for a source normalization, oldest attempt first."""
        result = await self._session.execute(
            self._with_children(
                select(ValidationAttempt)
                .where(ValidationAttempt.normalization_id == normalization_id)
                .order_by(ValidationAttempt.attempt_number)
            )
        )
        return result.scalars().all()

    async def latest_for_normalization(
        self, normalization_id: uuid.UUID
    ) -> ValidationAttempt | None:
        """The most recent attempt (highest ``attempt_number``) for a normalization."""
        result = await self._session.execute(
            self._with_children(
                select(ValidationAttempt)
                .where(ValidationAttempt.normalization_id == normalization_id)
                .order_by(ValidationAttempt.attempt_number.desc())
                .limit(1)
            )
        )
        return result.scalar_one_or_none()

    async def active_for_normalization(
        self, normalization_id: uuid.UUID
    ) -> ValidationAttempt | None:
        """The normalization's ``PROCESSING`` attempt, if one exists.

        At most one can exist - a partial unique index on
        ``invoice_validations`` enforces it at the database level.
        """
        result = await self._session.execute(
            select(ValidationAttempt).where(
                ValidationAttempt.normalization_id == normalization_id,
                ValidationAttempt.status == ValidationStatus.PROCESSING,
            )
        )
        return result.scalar_one_or_none()

    async def next_attempt_number(self, normalization_id: uuid.UUID) -> int:
        """``1`` for a normalization's first attempt, otherwise ``max + 1``."""
        result = await self._session.execute(
            select(
                func.coalesce(func.max(ValidationAttempt.attempt_number), 0) + 1
            ).where(ValidationAttempt.normalization_id == normalization_id)
        )
        return int(result.scalar_one())

    # --- writes -------------------------------------------------------- --

    def add_attempt(
        self, *, normalization_id: uuid.UUID, attempt_number: int
    ) -> ValidationAttempt:
        """Create a new ``PROCESSING`` attempt and stage it on the session.

        The caller flushes; the findings are filled in later by
        :meth:`apply_result` once a result exists.
        """
        attempt = ValidationAttempt(
            normalization_id=normalization_id,
            attempt_number=attempt_number,
            status=ValidationStatus.PROCESSING,
            started_at=_utcnow(),
        )
        self._session.add(attempt)
        return attempt

    def apply_result(
        self, attempt: ValidationAttempt, result: InvoiceValidation
    ) -> None:
        """Write a validated result onto ``attempt``.

        Does not change ``attempt.status`` or commit - the service does that
        after this returns, so a half-applied result is never marked
        ``COMPLETED`` and a failing write can be rolled back whole.
        """
        attempt.findings = [
            ValidationFindingRow(**row) for row in finding_rows(result)
        ]


__all__ = ["ValidationRepository"]
