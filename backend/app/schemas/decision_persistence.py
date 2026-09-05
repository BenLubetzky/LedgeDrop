"""Mapping between the ``InvoiceDecision`` contract and the flat
``invoice_decision_reasons`` column layout (Stage 6, package 2).

The *internal* contract (:class:`app.schemas.decision.InvoiceDecision`) is an
outcome plus a list of :class:`~app.schemas.decision.DecisionReason`. The
*persistence* layout (:mod:`app.models.decision`) is one
``invoice_decision_reasons`` row per reason, ordered by ``position`` (Stage 5
catalogue order, then a manual-review reason last, exactly the order the
package 3 engine emits them in); ``outcome`` is stored directly on
``invoice_decisions`` rather than re-derived, per that model's docstring.

This module is the single bridge between those two shapes, mirroring
:mod:`app.schemas.validation_persistence`. Unlike a Stage 5 finding, a
:class:`~app.schemas.decision.DecisionReason` does not by itself carry which
exact ``invoice_validation_findings`` row it explains - that reference
(``source_finding_id``) lives only in persistence, so it travels alongside
the reasons as a separate, position-aligned sequence rather than through the
pure contract.

There is no AI here and no database access - pure structural transformation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.schemas.decision import DecisionReason, InvoiceDecision

# The columns of one persisted reason, in ORM order (besides identity /
# bookkeeping columns: ``decision_reason_id``, ``decision_id``, ``created_at``,
# and ``position`` itself, which is derived from list order).
REASON_FIELD_NAMES: tuple[str, ...] = (
    "code",
    "triggers_review",
    "source_rule",
    "source_finding_id",
    "field_path",
    "message",
)

__all__ = [
    "REASON_FIELD_NAMES",
    "reason_rows",
    "invoice_decision_from_rows",
]


# --- flatten ----------------------------------------------------------


def reason_rows(
    decision: InvoiceDecision,
    source_finding_ids: Sequence[uuid.UUID | None],
) -> list[dict[str, Any]]:
    """Flatten ``decision.reasons`` into ordered ``invoice_decision_reasons`` rows.

    ``source_finding_ids`` gives the originating ``invoice_validation_findings``
    row id for each reason, positionally aligned with ``decision.reasons`` -
    the package 3 engine builds it alongside the reasons themselves, since
    only it has both the ``ValidationFinding`` and its stored row id in hand.
    An entry is ``None`` exactly for a ``manual_review_requested`` reason, and
    must be a UUID for every other (rule-derived) reason - the same rule
    :class:`~app.schemas.decision.DecisionReason` itself enforces between its
    own ``code`` and ``source_rule``.

    Each dict includes a zero-based ``position`` matching list order, plus the
    six :data:`REASON_FIELD_NAMES`. An empty reason list yields an empty list.
    """
    if len(source_finding_ids) != len(decision.reasons):
        raise ValueError(
            "source_finding_ids must have exactly one entry per reason "
            f"({len(source_finding_ids)} given for {len(decision.reasons)} reasons)"
        )
    rows: list[dict[str, Any]] = []
    for position, (reason, finding_id) in enumerate(
        zip(decision.reasons, source_finding_ids)
    ):
        if (finding_id is None) != (reason.source_rule is None):
            raise ValueError(
                "source_finding_id must be present exactly when source_rule is "
                f"present (reason at position {position}, code={reason.code!r})"
            )
        rows.append(
            {
                "position": position,
                "code": reason.code,
                "triggers_review": reason.triggers_review,
                "source_rule": reason.source_rule.value if reason.source_rule else None,
                "source_finding_id": finding_id,
                "field_path": reason.field_path,
                "message": reason.message,
            }
        )
    return rows


# --- rebuild -----------------------------------------------------------


def _get(source: Any, key: str) -> Any:
    """Read ``key`` from an ORM row (attribute) or a plain mapping (item)."""
    if isinstance(source, Mapping):
        return source[key]
    return getattr(source, key)


def invoice_decision_from_rows(rows: Iterable[Any]) -> InvoiceDecision:
    """Rebuild the contract payload from persisted, position-ordered rows.

    ``rows`` are a stored attempt's reasons (ORM objects or mappings). Only
    the contract's own five fields are read back (``source_finding_id`` stays
    persistence-only); the data is run back through
    :class:`~app.schemas.decision.DecisionReason` /
    :class:`InvoiceDecision`, so the closed code enum, the ``source_rule`` /
    ``code`` agreement, the ``field_path`` shape, and the re-derived
    ``outcome`` are all re-enforced on the way out.
    """
    ordered = sorted(rows, key=lambda row: _get(row, "position"))
    positions = [_get(row, "position") for row in ordered]
    if positions != list(range(len(ordered))):
        raise ValueError(
            "decision reason positions must be unique and contiguous from zero"
        )
    reasons = [
        DecisionReason.model_validate(
            {
                "code": _get(row, "code"),
                "triggers_review": _get(row, "triggers_review"),
                "source_rule": _get(row, "source_rule"),
                "field_path": _get(row, "field_path"),
                "message": _get(row, "message"),
            }
        )
        for row in ordered
    ]
    return InvoiceDecision.from_reasons(reasons)
