"""Migration round-trip for ``0006_review_tables`` (Stage 7, package 1).

Drives the real ``alembic`` CLI (not the ORM's ``create_all``) through
upgrade -> downgrade -> upgrade against a dedicated throwaway PostgreSQL
database, and proves:

* every Stage 2-6 row survives the cycle byte-for-byte;
* the ``document_status`` enum loses ``APPROVED``/``REJECTED`` on downgrade and
  regains them on re-upgrade (the rename-swap in the migration is reversible);
* ``alembic check`` is clean at head.

Mirrors
``test_stage6_verification.py::test_migration_upgrade_downgrade_preserves_stage2_5_data``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import (
    DecisionAttempt,
    DecisionOutcome,
    DecisionReasonRow,
    DecisionStatus,
    Document,
    DocumentStatus,
    ExtractionAttempt,
    ExtractionLineItem,
    ExtractionStatus,
    FindingSeverity,
    NormalizationAttempt,
    NormalizationStatus,
    ReviewAction,
    ReviewRecord,
    ValidationAttempt,
    ValidationFindingRow,
    ValidationRule,
    ValidationStatus,
)
from app.schemas.decision_catalogue import POLICY_VERSION
from app.schemas.review import REVIEW_POLICY_VERSION

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_STAGE2_6_TABLES = (
    "documents",
    "invoice_extractions",
    "invoice_line_items",
    "invoice_normalizations",
    "invoice_normalized_line_items",
    "invoice_normalization_errors",
    "invoice_validations",
    "invoice_validation_findings",
    "invoice_decisions",
    "invoice_decision_reasons",
)


def _throwaway_url() -> str:
    from app.core.config import settings

    url = sa.engine.make_url(settings.database_url)
    return url.set(
        database=f"ledgerdrop_stage7_migration_{uuid.uuid4().hex}"
    ).render_as_string(hide_password=False)


async def _set_database(url: str, *, exists: bool) -> None:
    target = sa.engine.make_url(url)
    admin_url = target.set(database="postgres").render_as_string(hide_password=False)
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with admin.connect() as conn:
            await conn.execute(
                sa.text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)')
            )
            if exists:
                await conn.execute(sa.text(f'CREATE DATABASE "{target.database}"'))
    finally:
        await admin.dispose()


def _run_alembic(*args: str, database_url: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(_BACKEND_DIR),
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


async def _enum_labels(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                sa.text("SELECT unnest(enum_range(NULL::document_status))::text")
            )
            return set(rows.scalars())
    finally:
        await engine.dispose()


async def _snapshot(url: str) -> dict[str, list[dict]]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            out: dict[str, list[dict]] = {}
            for table in _STAGE2_6_TABLES:
                res = await conn.execute(sa.text(f'SELECT * FROM "{table}" ORDER BY 1'))
                out[table] = [dict(row) for row in res.mappings().all()]
            return out
    finally:
        await engine.dispose()


async def _seed(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            doc = Document(
                original_filename="invoice.pdf",
                file_location=f"{uuid.uuid4()}/original.pdf",
                file_hash="a" * 64,
                file_size_bytes=2048,
                page_count=1,
                status=DocumentStatus.NEEDS_REVIEW,
            )
            session.add(doc)
            await session.flush()
            extraction = ExtractionAttempt(
                document_id=doc.document_id,
                attempt_number=1,
                status=ExtractionStatus.COMPLETED,
                completed_at=datetime.now(timezone.utc),
                provider_name="fake",
                invoice_number_value="INV-1",
                total_amount_value=Decimal("100.00"),
            )
            session.add(extraction)
            await session.flush()
            session.add(
                ExtractionLineItem(
                    extraction_id=extraction.extraction_id,
                    position=0,
                    description_value="Widget",
                    quantity_value=Decimal("1"),
                    unit_price_value=Decimal("100.00"),
                    line_total_value=Decimal("100.00"),
                )
            )
            normalization = NormalizationAttempt(
                extraction_id=extraction.extraction_id,
                attempt_number=1,
                status=NormalizationStatus.COMPLETED,
                completed_at=datetime.now(timezone.utc),
                invoice_number="INV-1",
                currency="EUR",
                total_amount=Decimal("100.00"),
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
            finding = ValidationFindingRow(
                validation_id=validation.validation_id,
                position=0,
                rule=ValidationRule.PROBABLE_DUPLICATE_INVOICE,
                severity=FindingSeverity.WARNING,
                field_path=None,
                expected=None,
                actual=None,
                message="This invoice looks like a probable duplicate.",
                context={},
            )
            session.add(finding)
            await session.flush()
            decision = DecisionAttempt(
                validation_id=validation.validation_id,
                attempt_number=1,
                status=DecisionStatus.COMPLETED,
                outcome=DecisionOutcome.NEEDS_REVIEW,
                policy_version=POLICY_VERSION,
                completed_at=datetime.now(timezone.utc),
            )
            decision.reasons = [
                DecisionReasonRow(
                    position=0,
                    code="probable_duplicate_invoice",
                    triggers_review=True,
                    source_rule="probable_duplicate_invoice",
                    source_finding_id=finding.validation_finding_id,
                    field_path=None,
                    message="This invoice looks like a probable duplicate.",
                )
            ]
            session.add(decision)
            await session.flush()
            # A Stage 7 review row exists before the cycle; it is a Stage 7
            # table, so the downgrade/upgrade drops and recreates it empty -
            # only Stage 2-6 rows must survive byte-for-byte.
            session.add(
                ReviewRecord(
                    decision_id=decision.decision_id,
                    action=ReviewAction.REJECT,
                    reviewer_name="Dana Ops",
                    note="confirmed duplicate of INV-1",
                    policy_version=REVIEW_POLICY_VERSION,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


async def test_migration_round_trip_preserves_stage2_6_data_and_is_reversible() -> None:
    url = _throwaway_url()
    await _set_database(url, exists=True)
    try:
        _run_alembic("upgrade", "head", database_url=url)
        assert {"APPROVED", "REJECTED"} <= await _enum_labels(url)

        await _seed(url)
        before = await _snapshot(url)
        assert len(before["invoice_decisions"]) == 1
        assert len(before["invoice_decision_reasons"]) == 1

        _run_alembic("downgrade", "-1", database_url=url)
        assert {"APPROVED", "REJECTED"}.isdisjoint(await _enum_labels(url))

        _run_alembic("upgrade", "head", database_url=url)
        _run_alembic("check", database_url=url)
        assert {"APPROVED", "REJECTED"} <= await _enum_labels(url)

        after = await _snapshot(url)
        for table in _STAGE2_6_TABLES:
            assert after[table] == before[table], f"{table} changed across the cycle"
    finally:
        await _set_database(url, exists=False)
