"""Internal invoice extraction data contract (Stage 3, step 1).

This module is pure data definition. It describes the schema-constrained shape
that every extraction provider response must be parsed and validated into before
anything is persisted or returned. **No AI provider is called here.**

It is the *internal* contract - not the public API response and not a database
row. Later Stage 3 steps derive those from :class:`InvoiceExtraction`.

Design rules (see ``docs/stage-3-extraction.md``):

* Every field carries an explicit ``{value, confidence}`` pair. Both keys are
  required in an incoming payload; either may be ``null``.
* A missing extracted value is ``null`` - never an empty string, never invented.
* ``confidence`` is a ``Decimal`` in ``[0, 1]`` inclusive, or ``null``.
* There is no document-level confidence score.
* Money amounts and quantities are ``Decimal`` and reject NaN/Infinity. Pydantic
  serializes ``Decimal`` as a JSON string, so values round-trip without binary
  floating-point artifacts.
* Invoice numbers and tax IDs stay strings.
* Dates remain raw strings. Their format, meaning, and calendar validity are
  checked by the later normalization stage, not by this structural contract.
* Currency uses the conventional 3-letter alphabetic shape and is upper-cased.
  Whether the code is recognized is checked later; amounts are never converted
  and a missing currency is never defaulted.
* Unknown keys are rejected (``extra="forbid"``) so arbitrary provider prose
  cannot become application state.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Generic, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

T = TypeVar("T")

# --- constrained leaf types --------------------------------------------------

Confidence = Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(1), allow_inf_nan=False)]
"""Per-field confidence: a Decimal in [0, 1] inclusive. Nullable at use sites."""

Money = Annotated[Decimal, Field(allow_inf_nan=False)]
"""A monetary amount. Sign is not constrained (credits/adjustments are valid)."""

Quantity = Annotated[Decimal, Field(allow_inf_nan=False)]
"""A line-item quantity. Sign is not constrained (returns are valid)."""

_CURRENCY_RE = re.compile(r"[A-Za-z]{3}")


def _require_currency_code(raw: str) -> str:
    if not _CURRENCY_RE.fullmatch(raw):
        raise ValueError("currency must use a 3-letter alphabetic code")
    return raw.upper()


RawDate = str
"""A date exactly as extracted; interpretation belongs to normalization."""

CurrencyCode = Annotated[str, AfterValidator(_require_currency_code)]
"""A conventional 3-letter currency code, normalized to upper case."""


# --- the value + confidence envelope --------------------------------------- -


class ExtractedField(BaseModel, Generic[T]):
    """One extracted value together with its own confidence.

    Both keys must be present in an incoming payload; either may be ``null``.
    ``value`` is ``None`` when the field was not found in the source document.
    """

    model_config = ConfigDict(extra="forbid")

    value: T | None
    confidence: Confidence | None


# --- the invoice contract ------------------------------------------------- ---


class ExtractedLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: ExtractedField[str]
    quantity: ExtractedField[Quantity]
    unit_price: ExtractedField[Money]
    line_total: ExtractedField[Money]


class InvoiceExtraction(BaseModel):
    """The full schema-constrained invoice extraction result.

    Every scalar field is required to be *present* (so a provider adapter must
    account for all of them), but each carries a nullable value and a nullable
    confidence. ``line_items`` may be an empty list.
    """

    model_config = ConfigDict(extra="forbid")

    invoice_number: ExtractedField[str]
    invoice_date: ExtractedField[RawDate]
    due_date: ExtractedField[RawDate]
    vendor_name: ExtractedField[str]
    vendor_tax_id: ExtractedField[str]
    customer_name: ExtractedField[str]
    currency: ExtractedField[CurrencyCode]
    subtotal: ExtractedField[Money]
    tax_amount: ExtractedField[Money]
    total_amount: ExtractedField[Money]
    line_items: list[ExtractedLineItem]


# Fields that later decision logic treats as critical. Recorded here for
# reference only; Stage 3 does not threshold or escalate on them.
CRITICAL_FIELDS: frozenset[str] = frozenset(
    {"invoice_number", "invoice_date", "vendor_name", "currency", "total_amount"}
)

__all__ = [
    "Confidence",
    "Money",
    "Quantity",
    "RawDate",
    "CurrencyCode",
    "ExtractedField",
    "ExtractedLineItem",
    "InvoiceExtraction",
    "CRITICAL_FIELDS",
]
