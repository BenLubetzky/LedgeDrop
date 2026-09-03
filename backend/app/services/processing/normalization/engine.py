"""Deterministic normalization engine (Stage 4, step 11).

Maps a validated Stage 3 :class:`~app.schemas.extraction.InvoiceExtraction`
onto the Stage 4 :class:`~app.schemas.normalization.NormalizedInvoice` contract
by running the field normalizers from :mod:`.normalizers` over every scalar
field and every line item.

* Each field's raw value comes from the extraction's ``{value, confidence}``
  envelope; the confidence is ignored - Stage 4 neither reads nor recomputes
  it.
* A field that normalizes cleanly contributes its canonical value, or ``None``
  for a clean absence (the source was ``null``/empty/blank).
* A field-level normalization failure contributes ``None`` **and** one
  :class:`~app.schemas.normalization.NormalizationError` carrying the stable
  ``field_path``, the stringified ``raw_value``, the closed ``code``, and the
  client-safe ``message``. That is *data about the invoice*, never a technical
  failure of the attempt.

The assembled result is run back through :class:`NormalizedInvoice`, so the
``YYYY-MM-DD`` date shape, the 3-letter currency shape, decimal typing, the
closed error-code set, and the "a field with an error must be null" rule are all
re-enforced here.

**No AI, no I/O, no network.** A contract-invalid input (for example a non-str
where the Stage 3 contract promises ``str | None``) makes a normalizer raise;
the caller (:class:`~app.services.processing.normalization.service.NormalizationService`)
turns that into a technical failure.
"""

from __future__ import annotations

from collections.abc import Callable

from app.schemas.extraction import InvoiceExtraction
from app.schemas.normalization import (
    NORMALIZED_LINE_ITEM_FIELD_NAMES,
    NORMALIZED_SCALAR_FIELD_NAMES,
    NormalizationError,
    NormalizedInvoice,
)
from app.services.processing.normalization.normalizers import (
    MAX_LINE_ITEM_DESCRIPTION,
    MAX_PARTY_NAME,
    NormResult,
    normalize_currency,
    normalize_date,
    normalize_invoice_number,
    normalize_money,
    normalize_quantity,
    normalize_tax_id,
    normalize_text,
)

_Normalizer = Callable[[object | None], NormResult]

# Which normalizer runs for each scalar field, in contract order. The two party
# names need an explicit length cap, so they are wrapped to the common
# one-argument shape.
_SCALAR_NORMALIZERS: dict[str, _Normalizer] = {
    "invoice_number": normalize_invoice_number,
    "invoice_date": normalize_date,
    "due_date": normalize_date,
    "vendor_name": lambda raw: normalize_text(raw, max_length=MAX_PARTY_NAME),
    "vendor_tax_id": normalize_tax_id,
    "customer_name": lambda raw: normalize_text(raw, max_length=MAX_PARTY_NAME),
    "currency": normalize_currency,
    "subtotal": normalize_money,
    "tax_amount": normalize_money,
    "total_amount": normalize_money,
}

_LINE_ITEM_NORMALIZERS: dict[str, _Normalizer] = {
    "description": lambda raw: normalize_text(
        raw, max_length=MAX_LINE_ITEM_DESCRIPTION
    ),
    "quantity": normalize_quantity,
    "unit_price": normalize_money,
    "line_total": normalize_money,
}

# The engine must account for exactly the fields the contract defines - no more,
# no fewer - and in the same order, so an error path index lines up with the
# persisted line-item order.
assert tuple(_SCALAR_NORMALIZERS) == NORMALIZED_SCALAR_FIELD_NAMES
assert tuple(_LINE_ITEM_NORMALIZERS) == NORMALIZED_LINE_ITEM_FIELD_NAMES


def _raw_value_repr(raw: object | None) -> str | None:
    """The offending source value, stringified for the error record.

    ``None`` (an absent source value) is recorded as ``null``; every other
    value is rendered with ``str`` so a ``Decimal`` keeps its exact digits and
    scale.
    """
    return None if raw is None else str(raw)


def _apply(
    field_path: str,
    raw: object | None,
    normalizer: _Normalizer,
    errors: list[NormalizationError],
) -> object | None:
    """Run one normalizer; append an error on failure and return the value."""
    result = normalizer(raw)
    if result.error is not None:
        errors.append(
            NormalizationError(
                field_path=field_path,
                raw_value=_raw_value_repr(raw),
                code=result.error.code,
                message=result.error.message,
            )
        )
        return None
    return result.value


def normalize_extraction(extraction: InvoiceExtraction) -> NormalizedInvoice:
    """Normalize one extraction contract into the canonical invoice contract.

    Deterministic and total: every scalar field and every line item is
    accounted for, and the returned :class:`NormalizedInvoice` is already
    schema-valid. Field-level failures are collected in ``errors``; they do not
    stop the pass.
    """
    errors: list[NormalizationError] = []

    scalars: dict[str, object | None] = {
        name: _apply(name, getattr(extraction, name).value, normalizer, errors)
        for name, normalizer in _SCALAR_NORMALIZERS.items()
    }

    line_items: list[dict[str, object | None]] = [
        {
            name: _apply(
                f"line_items.{index}.{name}",
                getattr(item, name).value,
                normalizer,
                errors,
            )
            for name, normalizer in _LINE_ITEM_NORMALIZERS.items()
        }
        for index, item in enumerate(extraction.line_items)
    ]

    return NormalizedInvoice.model_validate(
        {**scalars, "line_items": line_items, "errors": errors}
    )


__all__ = ["normalize_extraction"]
