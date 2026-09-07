"""Mapping between the ``InvoiceReview`` contract and the flat
``invoice_reviews`` column layout (Stage 7, package 1).

The *internal* contract (:class:`app.schemas.review.InvoiceReview`) is an
action, a reviewer name, and an optional note. The *persistence* layout
(:mod:`app.models.review`) is one ``invoice_reviews`` row with those three
columns plus identity/bookkeeping columns the service fills in
(``review_id``, ``decision_id``, ``policy_version``, ``reviewed_at``,
``created_at``).

This module is the single bridge between those two shapes, mirroring
:mod:`app.schemas.decision_persistence`. There is one row per review and no
child table, so the mapping is a flat dict - but it still lives here so the
column layout is derived from a validated contract in exactly one place, and
the rebuild runs the data back through :class:`InvoiceReview` so the closed
action enum and the note rule are re-enforced on the way out.

There is no AI here and no database access - pure structural transformation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.schemas.review import InvoiceReview

# The columns of one persisted review that come from the contract (besides the
# identity / bookkeeping columns the service sets: review_id, decision_id,
# policy_version, reviewed_at, created_at).
REVIEW_COLUMN_NAMES: tuple[str, ...] = ("action", "reviewer_name", "note")

__all__ = ["REVIEW_COLUMN_NAMES", "review_row", "invoice_review_from_row"]


def review_row(review: InvoiceReview) -> dict[str, Any]:
    """Flatten a validated :class:`InvoiceReview` to its three contract columns.

    ``action`` is returned as the :class:`~app.schemas.review.ReviewAction`
    enum member (the ORM column binds it directly); ``reviewer_name`` and
    ``note`` are the already-trimmed values the contract produced.
    """
    return {
        "action": review.action,
        "reviewer_name": review.reviewer_name,
        "note": review.note,
    }


def _get(source: Any, key: str) -> Any:
    """Read ``key`` from an ORM row (attribute) or a plain mapping (item)."""
    if isinstance(source, Mapping):
        return source[key]
    return getattr(source, key)


def invoice_review_from_row(row: Any) -> InvoiceReview:
    """Rebuild the contract payload from a persisted row (ORM object or mapping).

    Only the contract's own three fields are read back; the data is run through
    :class:`InvoiceReview` again, so the closed action enum, the trimmed/bounded
    ``reviewer_name``, and the "note required for REJECT" rule are all
    re-enforced.
    """
    return InvoiceReview.model_validate(
        {
            "action": _get(row, "action"),
            "reviewer_name": _get(row, "reviewer_name"),
            "note": _get(row, "note"),
        }
    )
