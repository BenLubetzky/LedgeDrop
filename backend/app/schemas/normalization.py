"""Internal normalized invoice data contract (Stage 4, step 1).

This module is pure data definition. It describes the schema-constrained shape
that a completed Stage 3 extraction is normalized *into*. **No AI provider is
called here, and there is no database access.**

It is the *internal* contract - not the public API response and not a database
row. Later Stage 4 steps derive the persistence layout (step 3), the
nested/flat bridge (step 5), and the public schemas (step 5) from these models.

How it differs from the Stage 3 extraction contract
(:mod:`app.schemas.extraction`):

* Extraction fields carry a ``{value, confidence}`` pair. Normalized fields
  carry a single *canonical* value or ``null`` - confidence is not repeated
  here. It stays on the extraction record and is joined in at the API layer if
  a response needs it (see ``docs/stage-4-normalization.md``). This keeps
  normalized data cleanly separate from the raw Stage 3 data.
* A normalized value is ``null`` when the source value was ``null``/empty *or*
  when the value could not be normalized. The second case additionally records
  a :class:`NormalizationError` in :attr:`NormalizedInvoice.errors`.
* Dates are canonical ``YYYY-MM-DD`` strings that denote a real calendar date.
  Impossible or ambiguous source dates do not appear here; they become an
  error instead.
* Currency is a 3-letter alphabetic code, upper-cased. Whether the code is on
  the approved ISO 4217 list is enforced by the step 8 normalizer, not by this
  structural contract; a missing currency stays ``null`` and is never
  defaulted or converted.
* Money amounts and quantities are ``Decimal`` and reject NaN/Infinity.
  Pydantic serializes ``Decimal`` as a JSON string, so values round-trip
  without binary floating-point artifacts.
* Invoice numbers and tax IDs stay strings.
* Empty or whitespace-only text is rejected: such a value must be ``null``,
  never ``""``.
* Unknown keys are rejected (``extra="forbid"``).

Structured normalization errors record four things, and this was a deliberate
decision for step 1:

* ``field_path`` - a stable path (e.g. ``total_amount`` or
  ``line_items.0.unit_price``) so a flat error list correlates to fields
  without nesting and covers line items by index.
* ``raw_value`` - the offending source value, stringified, captured at error
  time so the normalized result is self-contained for display and debugging
  without re-reading the Stage 3 record.
* ``code`` - a stable machine token from a closed set, for programmatic
  handling by later stages and for UI grouping.
* ``message`` - a client-safe sentence for display. It never contains internal
  paths, secrets, or stack traces.

The normalized result also preserves a reference to the source extraction
attempt via :class:`NormalizedInvoiceResult`. The pure data model
(:class:`NormalizedInvoice`) stays identity-free so it can be reused; the
persistence identity added later (``normalization_id``, status, timestamps)
lives in the step 3 / step 5 models.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from enum import Enum
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.schemas.extraction import Money, Quantity

__all__ = [
    "Money",
    "Quantity",
    "NormalizedDate",
    "NormalizedCurrencyCode",
    "NormalizedText",
    "NormalizationErrorCode",
    "NormalizationError",
    "NormalizedLineItem",
    "NormalizedInvoice",
    "NormalizedInvoiceResult",
    "NORMALIZED_SCALAR_FIELD_NAMES",
    "NORMALIZED_LINE_ITEM_FIELD_NAMES",
]


# --- constrained leaf types ------------------------------------------------

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CURRENCY_RE = re.compile(r"[A-Za-z]{3}")
_FIELD_PATH_RE = re.compile(r"[a-z_]+|line_items\.(?:0|[1-9]\d*)\.[a-z_]+")


def _require_iso_date(raw: str) -> str:
    """Accept only a canonical ``YYYY-MM-DD`` string for a real calendar date."""
    if not _ISO_DATE_RE.fullmatch(raw):
        raise ValueError("normalized date must be a canonical YYYY-MM-DD string")
    try:
        date.fromisoformat(raw)
    except ValueError as exc:  # e.g. 2026-02-30
        raise ValueError("normalized date is not a real calendar date") from exc
    return raw


def _require_currency_code(raw: str) -> str:
    if not _CURRENCY_RE.fullmatch(raw):
        raise ValueError("currency must use a 3-letter alphabetic code")
    return raw.upper()


def _require_non_empty_text(raw: str) -> str:
    """A normalized text value is already trimmed and must not be empty."""
    if raw != raw.strip():
        raise ValueError("normalized text must be trimmed")
    if not raw:
        raise ValueError("empty text must be represented as null, not \"\"")
    return raw


NormalizedDate = Annotated[str, AfterValidator(_require_iso_date)]
"""A canonical ``YYYY-MM-DD`` date string denoting a real calendar date."""

NormalizedCurrencyCode = Annotated[str, AfterValidator(_require_currency_code)]
"""A 3-letter alphabetic currency code, upper-cased. List check is step 8."""

NormalizedText = Annotated[str, AfterValidator(_require_non_empty_text)]
"""Trimmed, non-empty text. An empty result must be ``null`` instead."""


# --- structured normalization errors -------------------------------------


class NormalizationErrorCode(str, Enum):
    """Closed set of normalization error codes.

    This starter set covers the categories in ``docs/stage-4-normalization.md``.
    Step 2 (policy documentation) finalizes the authoritative list before the
    normalizers in step 6 are written; adding a member is a schema edit, not a
    migration.
    """

    INVALID_DATE = "invalid_date"
    AMBIGUOUS_DATE = "ambiguous_date"
    INVALID_CURRENCY = "invalid_currency"
    UNKNOWN_CURRENCY = "unknown_currency"
    INVALID_NUMBER = "invalid_number"
    AMBIGUOUS_NUMBER = "ambiguous_number"
    TEXT_TOO_LONG = "text_too_long"


class NormalizationError(BaseModel):
    """One field-level normalization failure.

    A field error is *data about the invoice*, not a technical failure of the
    normalization attempt (see ``docs/stage-4-normalization.md``). A
    ``COMPLETED`` attempt may carry several of these.
    """

    model_config = ConfigDict(extra="forbid")

    field_path: str = Field(min_length=1)
    raw_value: str | None
    code: NormalizationErrorCode
    message: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_field_path_shape(self) -> "NormalizationError":
        if not _FIELD_PATH_RE.fullmatch(self.field_path):
            raise ValueError(f"malformed field_path: {self.field_path!r}")
        return self


# --- the normalized invoice contract -----------------------------------


class NormalizedLineItem(BaseModel):
    """Canonical values for one line item.

    List order in :attr:`NormalizedInvoice.line_items` mirrors the source
    extraction's line-item order 1:1, so a line item is addressed by its index
    (``line_items.<index>.<field>``) in an error path.
    """

    model_config = ConfigDict(extra="forbid")

    description: NormalizedText | None
    quantity: Quantity | None
    unit_price: Money | None
    line_total: Money | None


class NormalizedInvoice(BaseModel):
    """The full schema-constrained normalized invoice result.

    Every scalar field must be *present* (a normalizer must account for all of
    them), but each carries a nullable canonical value. ``line_items`` may be an
    empty list. ``errors`` collects every field-level normalization failure and
    may also be empty. This model is identity-free; the link to the source
    extraction is on :class:`NormalizedInvoiceResult`.
    """

    model_config = ConfigDict(extra="forbid")

    invoice_number: NormalizedText | None
    invoice_date: NormalizedDate | None
    due_date: NormalizedDate | None
    vendor_name: NormalizedText | None
    vendor_tax_id: NormalizedText | None
    customer_name: NormalizedText | None
    currency: NormalizedCurrencyCode | None
    subtotal: Money | None
    tax_amount: Money | None
    total_amount: Money | None
    line_items: list[NormalizedLineItem]
    errors: list[NormalizationError]

    @model_validator(mode="after")
    def _check_error_paths_are_known(self) -> "NormalizedInvoice":
        item_count = len(self.line_items)
        for err in self.errors:
            head, _, rest = err.field_path.partition(".")
            if not rest:
                if head not in NORMALIZED_SCALAR_FIELD_NAMES:
                    raise ValueError(f"unknown error field_path: {err.field_path!r}")
                if getattr(self, head) is not None:
                    raise ValueError(
                        "a field with a normalization error must have a null value: "
                        f"{err.field_path!r}"
                    )
                continue
            index_str, _, leaf = rest.partition(".")
            if (
                head != "line_items"
                or not index_str.isdigit()
                or leaf not in NORMALIZED_LINE_ITEM_FIELD_NAMES
            ):
                raise ValueError(f"unknown error field_path: {err.field_path!r}")
            if int(index_str) >= item_count:
                raise ValueError(
                    f"error field_path references missing line item: {err.field_path!r}"
                )
            if getattr(self.line_items[int(index_str)], leaf) is not None:
                raise ValueError(
                    "a field with a normalization error must have a null value: "
                    f"{err.field_path!r}"
                )
        return self


class NormalizedInvoiceResult(BaseModel):
    """A :class:`NormalizedInvoice` bound to the extraction attempt it came from.

    Step 1 keeps this minimal: it preserves the reference to the source
    extraction and nothing else. The persistence identity (``normalization_id``,
    attempt number, status, timestamps, technical-failure fields) is added by
    the step 3 model and the step 5 schemas.
    """

    model_config = ConfigDict(extra="forbid")

    source_extraction_id: uuid.UUID
    normalized: NormalizedInvoice


# Field-name tuples derived from the models so downstream code (step 5 bridge,
# error-path validation above) cannot silently drift out of step with the
# contract. ``line_items`` and ``errors`` are the non-scalar top-level fields.
NORMALIZED_SCALAR_FIELD_NAMES: tuple[str, ...] = tuple(
    name
    for name in NormalizedInvoice.model_fields
    if name not in {"line_items", "errors"}
)
NORMALIZED_LINE_ITEM_FIELD_NAMES: tuple[str, ...] = tuple(
    NormalizedLineItem.model_fields
)
