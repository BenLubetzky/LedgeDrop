"""Mapping between the ``InvoiceValidation`` contract and the flat
``invoice_validation_findings`` column layout (Stage 5, step 11).

The *internal* contract (:class:`app.schemas.validation.InvoiceValidation`) is a
list of :class:`~app.schemas.validation.ValidationFinding` plus a re-derived
:class:`~app.schemas.validation.ValidationSummary`. The *persistence* layout
(:mod:`app.models.validation`) is one ``invoice_validation_findings`` row per
finding, ordered by ``position`` (the engine's rule-catalogue emission order);
``summary`` is never stored, only re-derived.

This module is the single bridge between those two shapes, mirroring
:mod:`app.schemas.normalization_persistence`. The repository (step 11) uses
:func:`finding_rows` to write a validated result into ORM rows; a later API
layer uses :func:`invoice_validation_from_rows` to rebuild the contract from a
stored attempt.

``expected`` / ``actual`` are stored as plain display text: a ``Decimal`` is
written as its canonical string (``str(value)``). The contract's
``str | Decimal`` union serialises to the same JSON string either way, so no
fidelity is lost for a client - rebuilding always yields a ``str``, never a
reconstructed ``Decimal``.

There is no AI here and no database access - pure structural transformation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from app.schemas.validation import InvoiceValidation, ValidationFinding

# The columns of one persisted finding, in ORM order (besides identity /
# bookkeeping columns: ``validation_finding_id``, ``validation_id``,
# ``created_at``, and ``position`` itself, which is derived from list order).
FINDING_FIELD_NAMES: tuple[str, ...] = (
    "rule",
    "severity",
    "field_path",
    "expected",
    "actual",
    "message",
    "context",
)

__all__ = [
    "FINDING_FIELD_NAMES",
    "finding_rows",
    "invoice_validation_from_rows",
]


# --- flatten ----------------------------------------------------------


def _display(value: str | Decimal | None) -> str | None:
    """``expected`` / ``actual`` as stored display text."""
    return None if value is None else str(value)


def finding_rows(result: InvoiceValidation) -> list[dict[str, Any]]:
    """Flatten ``result.findings`` into ordered ``invoice_validation_findings`` rows.

    Each dict includes a zero-based ``position`` matching list order, plus the
    seven :data:`FINDING_FIELD_NAMES`. An empty finding list yields an empty list.
    """
    rows: list[dict[str, Any]] = []
    for position, finding in enumerate(result.findings):
        rows.append(
            {
                "position": position,
                "rule": finding.rule,
                "severity": finding.severity,
                "field_path": finding.field_path,
                "expected": _display(finding.expected),
                "actual": _display(finding.actual),
                "message": finding.message,
                "context": finding.context,
            }
        )
    return rows


# --- rebuild -----------------------------------------------------------


def _get(source: Any, key: str) -> Any:
    """Read ``key`` from an ORM row (attribute) or a plain mapping (item)."""
    if isinstance(source, Mapping):
        return source[key]
    return getattr(source, key)


def invoice_validation_from_rows(rows: Iterable[Any]) -> InvoiceValidation:
    """Rebuild the contract payload from persisted, position-ordered rows.

    ``rows`` are a stored attempt's findings (ORM objects or mappings). The data
    is run back through :class:`ValidationFinding` / :class:`InvoiceValidation`,
    so the closed rule/severity enums, the ``field_path`` shape, and the
    re-derived ``summary`` are all re-enforced on the way out.
    """
    ordered = sorted(rows, key=lambda row: _get(row, "position"))
    positions = [_get(row, "position") for row in ordered]
    if positions != list(range(len(ordered))):
        raise ValueError(
            "validation finding positions must be unique and contiguous from zero"
        )
    findings = [
        ValidationFinding.model_validate(
            {name: _get(row, name) for name in FINDING_FIELD_NAMES}
        )
        for row in ordered
    ]
    return InvoiceValidation.from_findings(findings)
