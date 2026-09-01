# Stage 3: structured invoice extraction — full specification

This is the detailed specification for Stage 3. `CLAUDE.md` carries the
high-level summary and the guardrails; this file holds the field contract,
persistence layout, preprocessing steps, provider-evaluation criteria, API
scope, and the required test list. Read this before implementing Stage 3.

Stage 3 converts a stored PDF into schema-constrained invoice data with a
confidence value for each extracted field. It does **not** implement
normalization, business validation, escalation decisions, or human review.

## Objective

Stage 3 is complete when LedgerDrop can:

1. Start extraction for an existing `UPLOADED` document.
2. Transition the document to `PROCESSING` only when work actually starts.
3. Retrieve the stored PDF and preprocess text-based or scanned pages as needed.
4. Send the prepared content through a replaceable extraction provider.
5. Require structured output matching LedgerDrop's invoice schema.
6. Store extracted values, per-field confidences, provider metadata, and a raw
   provider response suitable for debugging.
7. Expose the extraction result through a safe API contract.
8. Mark a successful extraction `COMPLETED` for Stage 3, without implying that
   later normalization or validation has run.
9. Mark unrecoverable technical or extraction failures `FAILED` with a safe,
   useful reason.
10. Support an explicit retry without creating conflicting active results.

`NEEDS_REVIEW` is reserved for the later decision/escalation stage and must not
be assigned merely because extraction confidence is low.

## Required invoice extraction contract

The internal structured result contains these fields:

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

Each scalar field must carry both its extracted value and its own confidence.
Each line-item field must do the same. A conceptual shape is:

```json
{
  "value": "INV-28491",
  "confidence": 0.97
}
```

Contract rules:

- Confidence is a decimal from `0` to `1`, inclusive, or `null` when it cannot
  be supplied or derived reliably.
- There is no document-level confidence score.
- Missing values are `null`, not empty strings and never invented values.
- Invoice numbers and tax IDs remain strings.
- Dates may use `YYYY-MM-DD` only when the source is unambiguous; ambiguous raw
  values must not be silently interpreted. Full date normalization belongs to
  the next stage.
- Currency should be an extracted ISO code when clearly identified. Do not
  silently convert amounts or default a missing currency to EUR in extraction.
- Monetary values and quantities use decimals and must serialize without binary
  floating-point artifacts.
- Preserve source evidence such as page number or quoted region when the
  provider makes it available, but do not require speculative evidence data.
- Arbitrary model prose must never become application state. Parse and validate
  every provider response against the schema before persistence.

Initially important fields for later decision logic are:

```text
invoice_number
invoice_date
vendor_name
currency
total_amount
```

Stage 3 records confidence but does not discard fields, choose a confidence
threshold, or escalate low-confidence results.

## Extraction persistence

Add an extraction model linked to `documents.document_id`. Its exact normalized
table layout may be selected during implementation, but it must preserve:

```text
extraction_id
document_id
status
structured extracted fields with per-field confidence
raw provider response
provider name
provider model/version when available
started_at
completed_at
failure_code
failure_message
created_at
updated_at
```

Requirements:

- Add schema changes through Alembic migrations.
- Public responses must not expose secrets, internal paths, stack traces, or
  unsafe provider diagnostics.
- Raw provider responses are internal debugging/audit data and should not be
  returned by the normal public extraction endpoint.
- Define clearly whether retry updates an attempt or creates a new attempt.
  Prefer preserving attempt history if it can be done without unnecessary
  orchestration complexity.
- Avoid partial results: a malformed provider response must not be persisted as
  a successful extraction.
- Keep the original PDF immutable.

## Extraction preprocessing

The preprocessing layer should:

1. Safely resolve the stored PDF through the existing storage service.
2. Detect whether useful embedded text is available.
3. Extract embedded text for digital PDFs when appropriate.
4. Render pages for OCR/vision when the PDF is scanned or text extraction is
   inadequate.
5. Respect the existing 10-page limit and bound memory/resource use.
6. Produce provider-ready input without changing the stored original.

Do not assume every readable PDF has a text layer. Keep preprocessing separate
from provider invocation so either part can be tested and replaced.

## Extraction provider boundary

Create a small provider interface whose input is provider-ready document content
and whose output is the validated internal extraction contract. The rest of the
application must not depend directly on a provider SDK.

Before implementing the real adapter, document the provider choice and why it
fits:

- digital and scanned PDF support
- structured/schema-constrained output
- availability or derivation of field-level confidence
- English invoice performance
- maximum file/page support
- latency, cost, privacy, and data-retention behavior
- retry and rate-limit behavior
- local development and testability

Automated tests must use a deterministic fake provider and must not call or
require an external AI service. Keep provider credentials in environment
variables and never commit them.

## Processing orchestration and statuses

The Stage 3 lifecycle is:

```text
UPLOADED -> PROCESSING -> COMPLETED
                      `-> FAILED
FAILED   -> PROCESSING -> COMPLETED or FAILED  (explicit retry)
```

Rules:

- Do not set `PROCESSING` until extraction actually begins.
- Avoid two concurrent active extractions for the same document.
- A success requires a schema-valid extraction persisted in PostgreSQL.
- A failure must leave the document and original PDF intact.
- Store a safe failure code/message that can support retry and later operations.
- `COMPLETED` currently means extraction completed. If later stages reuse this
  document status, update the lifecycle deliberately rather than silently
  changing its meaning.
- A background queue may be introduced only if required for reliable request
  duration or provider latency. Do not add distributed queue infrastructure by
  default; prefer a simple explicit orchestration boundary first.

## Stage 3 API scope

Add API behavior for:

- Starting extraction for an existing document.
- Retrieving the latest extraction result and its per-field confidence values.
- Explicitly retrying a failed extraction.
- Returning `404` for unknown document or extraction IDs.
- Returning a clear conflict response when the document is already processing or
  cannot legally transition from its current status.

Choose concrete paths and request/response schemas before implementation and
document them in the backend README or API documentation. Keep existing Stage 2
document endpoints backward compatible unless a change is discussed first.

Frontend work for Stage 3 should remain limited to showing real processing
status and, if included, a minimal read-only extraction result. Do not build the
editable human-review workflow yet.

## Evaluation and tests

Create a small, manually verified invoice evaluation set containing
representative examples where legally and safely available:

- digitally generated PDFs
- scanned PDFs
- multi-page invoices
- missing optional fields
- multiple line items
- unusual but valid layouts
- low-quality or unusable input
- a readable PDF that is not an invoice

At minimum, automated tests must cover:

- Successful structured extraction with all supported fields.
- Missing optional fields represented as `null`.
- Per-field confidence validation, including bounds.
- Line-item extraction and confidence values.
- Digital-text and scanned-document preprocessing paths.
- Schema rejection for malformed provider output.
- Provider timeout, rate-limit, and general failure handling.
- Correct `UPLOADED -> PROCESSING -> COMPLETED` transition.
- Correct transition to `FAILED` with the original document preserved.
- Retry after failure.
- Rejection of duplicate or concurrent processing starts.
- Extraction retrieval and unknown-ID `404` responses.
- No partial successful record after preprocessing, provider, validation, or
  database failure.
- No external provider calls in the normal automated suite.

Measure extraction field accuracy against the evaluation set before choosing
confidence thresholds or building escalation rules. Separate critical-field and
line-item accuracy where possible.
