"""Mapping between the nested extraction contract and the flat column layout
(Stage 3, step 4).

The *internal* contract (:class:`app.schemas.extraction.InvoiceExtraction`) is
nested: every field is a ``{value, confidence}`` pair and ``line_items`` is a
list. The *persistence* layout (``invoice_extractions`` /
``invoice_line_items``, see :mod:`app.models.extraction`) is flat: one
``<name>_value`` and one ``<name>_confidence`` column per field.

This module is the single bridge between those two shapes. The repository/service
layer (step 5) uses it to write a validated contract into ORM rows; the API
layer (step 7) uses it to rebuild the contract from a stored attempt.

There is no AI here and no database access - pure structural transformation.

The scalar and line-item field names are derived from the contract models
themselves, so this mapping cannot silently drift out of step with the contract.
A test additionally asserts that every column name produced here exists on the
ORM tables.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.schemas.extraction import ExtractedField, ExtractedLineItem, InvoiceExtraction

# Contract field names, in contract order. ``line_items`` is the one non-scalar
# top-level field and is handled separately.
SCALAR_FIELD_NAMES: tuple[str, ...] = tuple(
    name for name in InvoiceExtraction.model_fields if name != "line_items"
)
LINE_ITEM_FIELD_NAMES: tuple[str, ...] = tuple(ExtractedLineItem.model_fields)


def _pair_to_columns(name: str, field: ExtractedField[Any]) -> dict[str, Any]:
    """``{"<name>_value": ..., "<name>_confidence": ...}`` for one contract field."""
    return {f"{name}_value": field.value, f"{name}_confidence": field.confidence}


def scalar_columns(extraction: InvoiceExtraction) -> dict[str, Any]:
    """Flatten the ten scalar fields into ``invoice_extractions`` column values.

    The result is exactly the ``*_value`` / ``*_confidence`` keyword arguments
    for an :class:`~app.models.extraction.ExtractionAttempt`; it carries no
    identity, status, provider, or timing columns.
    """
    columns: dict[str, Any] = {}
    for name in SCALAR_FIELD_NAMES:
        columns.update(_pair_to_columns(name, getattr(extraction, name)))
    return columns


def line_item_columns(extraction: InvoiceExtraction) -> list[dict[str, Any]]:
    """Flatten ``line_items`` into ``invoice_line_items`` column values.

    Each dict includes a zero-based ``position`` matching the order in the
    contract, plus the ``*_value`` / ``*_confidence`` pairs. An empty
    ``line_items`` list yields an empty list.
    """
    rows: list[dict[str, Any]] = []
    for position, item in enumerate(extraction.line_items):
        row: dict[str, Any] = {"position": position}
        for name in LINE_ITEM_FIELD_NAMES:
            row.update(_pair_to_columns(name, getattr(item, name)))
        rows.append(row)
    return rows


def invoice_extraction_to_columns(
    extraction: InvoiceExtraction,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Convenience: ``(scalar_columns, line_item_columns)`` in one call."""
    return scalar_columns(extraction), line_item_columns(extraction)


def _get(source: Any, key: str) -> Any:
    """Read ``key`` from an ORM row (attribute) or a plain mapping (item)."""
    if isinstance(source, Mapping):
        return source[key]
    return getattr(source, key)


def _columns_to_pair(source: Any, name: str) -> dict[str, Any]:
    return {"value": _get(source, f"{name}_value"), "confidence": _get(source, f"{name}_confidence")}


def invoice_extraction_from_row(
    scalars: Any,
    line_item_rows: Iterable[Any],
) -> InvoiceExtraction:
    """Rebuild the nested contract from flat persisted data.

    ``scalars`` is a stored attempt (ORM object or mapping) and
    ``line_item_rows`` is its line items in ``position`` order. The data is run
    back through :class:`InvoiceExtraction`, so confidence bounds, decimal
    typing, currency shape, and unknown-key rejection are all re-enforced on the
    way out - a row that somehow violates the contract raises rather than
    reaching a client.
    """
    payload: dict[str, Any] = {
        name: _columns_to_pair(scalars, name) for name in SCALAR_FIELD_NAMES
    }
    payload["line_items"] = [
        {name: _columns_to_pair(row, name) for name in LINE_ITEM_FIELD_NAMES}
        for row in line_item_rows
    ]
    return InvoiceExtraction.model_validate(payload)


def invoice_extraction_from_attempt(attempt: Any) -> InvoiceExtraction:
    """Rebuild the contract from an ``ExtractionAttempt`` and its ``line_items``.

    Duck-typed on purpose (``attempt.<field>_value`` and ``attempt.line_items``)
    so this module keeps no import dependency on the ORM layer.
    """
    return invoice_extraction_from_row(attempt, attempt.line_items)


__all__ = [
    "SCALAR_FIELD_NAMES",
    "LINE_ITEM_FIELD_NAMES",
    "scalar_columns",
    "line_item_columns",
    "invoice_extraction_to_columns",
    "invoice_extraction_from_row",
    "invoice_extraction_from_attempt",
]
