# Later invoice-processing specification

This document is **future context only**. None of it may be implemented until the
user explicitly advances the project past Stage 2 (the upload foundation). It is
kept out of `CLAUDE.md` so it does not consume agent context during Stage 2 work.

## Planned extracted invoice fields

```text
invoice_number
invoice_date
due_date
vendor_name
vendor_tax_id
customer_name
currency
subtotal
tax_amount
total_amount
line_items[]
    description
    quantity
    unit_price
    line_total
```

Initially critical fields are expected to include:

```text
invoice_number
invoice_date
vendor_name
currency
total_amount
```

## Planned normalization expectations

- Dates use `YYYY-MM-DD`.
- Currencies use ISO codes such as `EUR`, `USD`, and `GBP`.
- When currency is absent, EUR may be used only if it is clearly recorded as a
  default or inference.
- Non-EUR invoice amounts must not be silently converted to EUR.
- Monetary amounts and quantities use decimal arithmetic, not binary floating
  point.
- Missing values use `null` rather than empty strings or invented values.
- Invoice numbers and tax IDs remain strings.

## Confidence and decisions

Confidence will be recorded per extracted field. Later decision logic will
discard or flag uncertain field values and escalate when a critical field is
missing or insufficiently trusted. A document-level confidence score is not
currently part of the design.

Planned escalation conditions include:

- Failed or unusable extraction
- A required field is missing
- Low confidence for a critical field
- Invoice totals differ beyond a small rounding tolerance
- Line items do not reconcile
- Invalid or inconsistent dates
- Probable duplicate invoice
- Invoice exceeds a configurable high-value threshold
- Explicit manual-review request

Exact confidence thresholds, rounding tolerances, and high-value limits remain
intentionally undecided until extraction can be evaluated using real invoices.
