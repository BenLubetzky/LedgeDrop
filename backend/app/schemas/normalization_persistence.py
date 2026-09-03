"""Mapping between the nested normalized-invoice contract and the flat
normalization column layout (Stage 4, step 5).

The *internal* contract (:class:`app.schemas.normalization.NormalizedInvoice`)
is nested: ten canonical scalar fields plus ``line_items`` and ``errors``
lists. The *persistence* layout (:mod:`app.models.normalization`) is flat:

* one column per scalar on ``invoice_normalizations`` - and unlike Stage 3
  there is **no** ``_value`` / ``_confidence`` split, because normalized data
  carries no confidence;
* one ``invoice_normalized_line_items`` row per line item, keyed by
  ``position``;
* one ``invoice_normalization_errors`` row per field-level error.

This module is the single bridge between those two shapes. The
repository/service layer (step 11) uses it to write a validated contract into
ORM rows; the API layer (step 12) uses it to rebuild the contract from a
stored attempt.

There is no AI here and no database access - pure structural transformation.
The scalar and line-item field names come from the contract models
themselves, so this mapping cannot silently drift out of step with the
contract. A test additionally asserts that every column name produced here
exists on the ORM tables.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.schemas.normalization import (
    NORMALIZED_LINE_ITEM_FIELD_NAMES,
    NORMALIZED_SCALAR_FIELD_NAMES,
    NormalizedInvoice,
)

# The columns of one persisted field error, in ORM order. ``normalization_id``,
# ``normalization_error_id`` and ``created_at`` are identity/bookkeeping and are
# not part of the contract.
ERROR_FIELD_NAMES: tuple[str, ...] = ("field_path", "raw_value", "code", "message")

__all__ = [
    "ERROR_FIELD_NAMES",
    "scalar_columns",
    "line_item_rows",
    "error_rows",
    "normalized_invoice_to_columns",
    "normalized_invoice_from_rows",
    "normalized_invoice_from_attempt",
]


# --- flatten --------------------------------------------------------------


def scalar_columns(normalized: NormalizedInvoice) -> dict[str, Any]:
    """The ten canonical scalar fields as ``invoice_normalizations`` column values.

    The result is exactly the scalar keyword arguments for a
    :class:`~app.models.normalization.NormalizationAttempt`; it carries no
    identity, status, or timing columns.
    """
    return {name: getattr(normalized, name) for name in NORMALIZED_SCALAR_FIELD_NAMES}


def line_item_rows(normalized: NormalizedInvoice) -> list[dict[str, Any]]:
    """Flatten ``line_items`` into ``invoice_normalized_line_items`` column values.

    Each dict includes a zero-based ``position`` matching contract order, plus
    the four canonical line-item fields. An empty ``line_items`` list yields an
    empty list.
    """
    rows: list[dict[str, Any]] = []
    for position, item in enumerate(normalized.line_items):
        row: dict[str, Any] = {"position": position}
        for name in NORMALIZED_LINE_ITEM_FIELD_NAMES:
            row[name] = getattr(item, name)
        rows.append(row)
    return rows


def error_rows(normalized: NormalizedInvoice) -> list[dict[str, Any]]:
    """Flatten ``errors`` into ``invoice_normalization_errors`` column values."""
    return [
        {name: getattr(err, name) for name in ERROR_FIELD_NAMES}
        for err in normalized.errors
    ]


def normalized_invoice_to_columns(
    normalized: NormalizedInvoice,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Convenience: ``(scalar_columns, line_item_rows, error_rows)`` in one call."""
    return (
        scalar_columns(normalized),
        line_item_rows(normalized),
        error_rows(normalized),
    )


# --- rebuild ------------------------------------------------------------


def _get(source: Any, key: str) -> Any:
    """Read ``key`` from an ORM row (attribute) or a plain mapping (item)."""
    if isinstance(source, Mapping):
        return source[key]
    return getattr(source, key)


def normalized_invoice_from_rows(
    scalars: Any,
    line_item_rows_in: Iterable[Any],
    error_rows_in: Iterable[Any],
) -> NormalizedInvoice:
    """Rebuild the nested contract from flat persisted data.

    ``scalars`` is a stored attempt (ORM object or mapping); the two iterables
    are its line items and its field errors. The data is
    run back through :class:`NormalizedInvoice`, so the ``YYYY-MM-DD`` date
    shape, the upper-case 3-letter currency shape, decimal typing, the closed
    error-code set, error-path validity, and the "a field with an error must be
    null" rule are all re-enforced on the way out - a row that somehow violates
    the contract raises rather than reaching a client.
    """
    rows_by_position = sorted(line_item_rows_in, key=lambda row: _get(row, "position"))
    positions = [_get(row, "position") for row in rows_by_position]
    if positions != list(range(len(rows_by_position))):
        raise ValueError(
            "normalized line-item positions must be unique and contiguous from zero"
        )

    payload: dict[str, Any] = {
        name: _get(scalars, name) for name in NORMALIZED_SCALAR_FIELD_NAMES
    }
    payload["line_items"] = [
        {name: _get(row, name) for name in NORMALIZED_LINE_ITEM_FIELD_NAMES}
        for row in rows_by_position
    ]
    payload["errors"] = [
        {name: _get(row, name) for name in ERROR_FIELD_NAMES} for row in error_rows_in
    ]
    return NormalizedInvoice.model_validate(payload)


def normalized_invoice_from_attempt(attempt: Any) -> NormalizedInvoice:
    """Rebuild the contract from a ``NormalizationAttempt`` and its collections.

    Duck-typed on purpose (``attempt.<field>``, ``attempt.line_items``,
    ``attempt.errors``) so this module keeps no import dependency on the ORM
    layer. ``line_items`` are ordered and checked by their persisted
    ``position``.
    """
    return normalized_invoice_from_rows(attempt, attempt.line_items, attempt.errors)
