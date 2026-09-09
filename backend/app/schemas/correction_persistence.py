"""Mapping between the correction contract and the flat correction column
layout (Stage 8, package 1).

The internal contract (:class:`app.schemas.correction.InvoiceCorrection`) is
reviewer attribution plus an ordered list of
:class:`~app.schemas.correction.CorrectionFieldEntry`. The persistence layout
(:mod:`app.models.correction`) is one ``invoice_corrections`` row plus one
``invoice_correction_fields`` row per entry, ``position``-numbered in list
order.

This module is the single bridge. No AI, no database access - pure structural
transformation. Rebuilding runs the data back through the contract, so the
closed ``operation`` / ``error_code`` enums and the ``field_path`` shape are
re-enforced on the way out.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.schemas.correction import (
    CORRECTION_FIELD_ROW_NAMES,
    CorrectionFieldEntry,
    InvoiceCorrection,
)

__all__ = [
    "correction_field_rows",
    "invoice_correction_from_row",
    "invoice_correction_from_attempt",
]


def correction_field_rows(entries: Iterable[CorrectionFieldEntry]) -> list[dict[str, Any]]:
    """Flatten contract entries into ``invoice_correction_fields`` column dicts."""
    rows: list[dict[str, Any]] = []
    for position, entry in enumerate(entries):
        row: dict[str, Any] = {"position": position}
        for name in CORRECTION_FIELD_ROW_NAMES:
            row[name] = getattr(entry, name)
        rows.append(row)
    return rows


def _get(source: Any, key: str) -> Any:
    if isinstance(source, Mapping):
        return source[key]
    return getattr(source, key)


def invoice_correction_from_row(
    attempt: Any, field_rows_in: Iterable[Any]
) -> InvoiceCorrection:
    """Rebuild the nested contract from a stored ``CorrectionAttempt`` + its rows.

    ``attempt`` supplies ``reviewer_name`` / ``note``; ``field_rows_in`` are
    its ``invoice_correction_fields`` rows (ORM objects or mappings). Rows are
    ordered by their persisted ``position`` and the result is re-validated
    through :class:`InvoiceCorrection`.
    """
    rows_by_position = sorted(field_rows_in, key=lambda row: _get(row, "position"))
    entries = [
        {name: _get(row, name) for name in CORRECTION_FIELD_ROW_NAMES}
        for row in rows_by_position
    ]
    return InvoiceCorrection.model_validate(
        {
            "reviewer_name": _get(attempt, "reviewer_name"),
            "note": _get(attempt, "note"),
            "entries": entries,
        }
    )


def invoice_correction_from_attempt(attempt: Any) -> InvoiceCorrection:
    """Rebuild the contract from a ``CorrectionAttempt`` and its ``fields``."""
    return invoice_correction_from_row(attempt, attempt.fields)
