# Stage 4: normalization — full specification

This is the detailed specification for Stage 4. `CLAUDE.md` carries the
high-level summary and the guardrails; this file holds the normalization
contract, the policy decisions that must be pinned before coding, the
persistence layout, the API scope, the implementation order, and the required
verification list. Read this before implementing Stage 4.

Stage 4 receives a completed Stage 3 extraction and produces a separate,
traceable normalized result. It does **not** implement business validation,
confidence thresholds, totals reconciliation, duplicate decisions,
acceptance/rejection, escalation, or human review.

## Objective

Stage 4 is complete when LedgerDrop can, for any `COMPLETED` extraction:

1. Load the raw, immutable Stage 3 extraction output.
2. Create a normalization attempt that references that source extraction.
3. Deterministically normalize every scalar field and every line item into
   canonical values, making no AI call and no external-network call.
4. Record a structured field error wherever a value is invalid or ambiguous,
   instead of guessing.
5. Persist normalized values and field errors transactionally, with attempt
   history preserved and at most one active attempt per source extraction.
6. Mark the attempt `COMPLETED` when normalization finished (field errors do
   not make the attempt fail) or `FAILED` for a genuine technical failure, with
   a safe reason.
7. Support explicit retry of a technically failed attempt without creating
   conflicting active attempts.
8. Expose the latest and specific normalized results through a safe API
   contract, with `404` for unknown IDs and `409` for illegal or concurrent
   starts.
9. Leave every Stage 3 raw value and the original PDF unchanged.

## Boundaries

Stage 4 does not:

- default a missing currency, convert currencies, or interpret exchange rates;
- reconcile `subtotal + tax_amount` against `total_amount` or reconcile line
  items against totals;
- apply confidence thresholds or discard low-confidence fields;
- decide duplicates, escalation, `NEEDS_REVIEW`, acceptance, or rejection;
- build any editable or human-review workflow;
- broaden input support beyond the existing PDF invoice scope.

A field-level normalization error is data about the invoice, not a technical
attempt failure. Only infrastructure-level problems (database unavailable,
source extraction unreadable, unexpected exception) make an attempt `FAILED`.

## Policies to pin before implementation

Each policy below must be decided and written into this document before the
step that depends on it is implemented. Do not silently invent policy in code.

- **Dates.** The exact set of accepted input formats; real-calendar validation;
  how ambiguous numeric dates (e.g. `03/04/2026`) are handled when locale is
  not established (expected: rejected with a structured error); output is
  always `YYYY-MM-DD`.
- **Numbers (money and quantities).** Accepted grouping and decimal separators;
  how ambiguous separators are resolved or rejected; sign handling for
  negatives; whether and when a precision/rounding rule applies (default: do
  not round); representation of malformed or ambiguous numbers as errors.
- **Currency.** The approved ISO 4217 code list; trim + upper-case behavior;
  the explicit, deterministic rules (if any) for interpreting currency
  symbols; missing currency is left `null` with no default.
- **Whitespace and empty values.** Trimming rules; collapsing of repeated
  internal whitespace; empty or whitespace-only values become `null`;
  meaningful punctuation is preserved.
- **Text limits.** Maximum stored length per text field and what happens on
  overflow (truncate vs. error).
- **Identifiers.** Invoice numbers and tax identifiers stay strings; which
  normalization (if any) is applied beyond whitespace trimming.
- **Failure representation.** The shape of a structured field error: stable
  field path, raw value, safe error code, safe message. The closed set of
  error codes.

## Normalization contract

For every completed extraction, Stage 4 produces:

- a normalized value for each scalar field in the extraction contract
  (`invoice_number`, `invoice_date`, `due_date`, `vendor_name`,
  `vendor_tax_id`, `customer_name`, `currency`, `subtotal`, `tax_amount`,
  `total_amount`);
- a normalized value for each field of each line item (`description`,
  `quantity`, `unit_price`, `line_total`);
- a list of structured field errors, each identifying the field path, the raw
  value, a safe error code, and a safe message.

Rules:

- A normalized value is `null` when the raw value was `null`/empty or when the
  value could not be normalized (in which case a field error is also
  recorded).
- Normalized dates are `YYYY-MM-DD` strings; invalid or ambiguous dates produce
  a field error and a `null` normalized value.
- Normalized money and quantity values are decimals and must serialize without
  binary floating-point artifacts.
- Per-field confidence from Stage 3 is carried through unchanged where the API
  needs it; Stage 4 neither recomputes nor acts on confidence.
- The normalized result references its source extraction attempt and never
  mutates it.

## Normalization persistence

Add normalization models linked to the Stage 3 extraction record. The exact
table layout may be chosen during implementation, but it must preserve:

```text
normalization_id
extraction_id            (source Stage 3 attempt; immutable)
status                   (PROCESSING | COMPLETED | FAILED)
normalized scalar fields
normalized line items
structured field errors  (field path, raw value, error code, safe message)
started_at
completed_at
failure_code             (technical failure only)
failure_message          (safe)
created_at
updated_at
```

Requirements:

- Schema changes go through Alembic migrations; verify upgrade and downgrade
  with existing Stage 2/Stage 3 data intact.
- Foreign keys, constraints, and indexes enforce the link to the source
  extraction, attempt history, and one active attempt per source extraction.
- Stage 3 records stay immutable.
- Public responses never expose internal exceptions, paths, secrets, or raw
  internal diagnostics.

## Schemas

Provide separate internal, persistence, request, and public response schemas.
Reject unknown keys. Serialize decimals without floating-point artifacts. The
public response exposes normalized values, structured field errors, the source
extraction reference, status, and timestamps only.

## Processing orchestration and statuses

```text
COMPLETED extraction -> (start) -> PROCESSING -> COMPLETED
                                            `-> FAILED
FAILED normalization -> (explicit retry) -> PROCESSING -> COMPLETED or FAILED
```

Rules:

- Do not set `PROCESSING` until normalization actually begins.
- Only one active normalization attempt per source extraction; reject
  concurrent or illegal starts with `409`.
- Field errors do not make an attempt `FAILED`; a `COMPLETED` attempt may
  contain field errors.
- A `FAILED` attempt leaves the source extraction and all raw values intact.
- Store a safe failure code/message that can support retry.
- Extraction and normalization remain independently callable during
  development even after the pipeline is connected.

## Stage 4 API scope

Add API behavior for:

- Starting normalization for a completed extraction.
- Retrieving the latest normalized result and, where useful, a specific
  attempt.
- Explicitly retrying a technically failed normalization.
- Attempt history where useful.
- `404` for unknown document, extraction, or normalization IDs.
- `409` when the extraction is not `COMPLETED`, is already normalizing, or
  cannot legally transition.

Choose concrete paths and request/response schemas before implementation and
document them in the backend README or API documentation. Stage 2 and Stage 3
endpoints stay backward compatible.

Frontend work for Stage 4 stays limited to showing normalization status and, if
included, a minimal read-only normalized result alongside the raw extraction.
Do not build an editable review workflow.

## Implementation order

Build Stage 4 in this sequence. Introduce no AI or external-network call at any
step. Detail for each step is filled in as the step is worked.

1. **Define the normalization contract.** Normalized invoice fields and line
   items, structured field errors, and the link to the source extraction
   attempt. Schema definition only.
2. **Document normalization policies.** Pin the dates, numbers, currencies,
   whitespace, empty-value, text-limit, and failure-representation policies in
   the "Policies to pin before implementation" section above.
3. **Design normalization persistence models.** Attempts, normalized values,
   normalized line items, errors, timestamps, status, and safe technical
   failure information. Stage 3 records stay immutable.
4. **Create and verify the Alembic migration.** Foreign keys, constraints,
   indexes, upgrade/downgrade, attempt history, and one-active-run protection,
   without damaging existing data.
5. **Create the schemas.** Separate internal, persistence, request, and public
   response models. Reject unknown keys; serialize decimals cleanly.
6. **Implement the deterministic normalizers.** Small units for dates,
   currency, money, quantities, general text, invoice numbers, and tax
   identifiers. Each returns a normalized value or a structured error and never
   calls AI.
7. **Implement date normalization.** Real-calendar validation; explicit
   rejection of ambiguous numeric dates when locale is not established.
8. **Implement currency normalization.** No defaulting, no conversion; ISO 4217
   list check; deterministic symbol rules only.
9. **Implement decimal normalization** for money and quantities using the
   documented separator and precision policies.
10. **Implement safe text normalization** while preserving identifier
    semantics.
11. **Build the normalization repository/service and lifecycle.** Load a
    completed extraction, create an attempt, normalize all fields and line
    items, store values and errors transactionally, support explicit retry of
    technical failures, and reject concurrent or illegal starts.
12. **Add the API endpoints.** Start normalization; retrieve latest or specific
    attempt; retry and history where useful. Clear `404` and `409` responses.
13. **Connect the pipeline** as `upload -> extraction -> normalization`, keeping
    extraction and normalization independently callable during development. Do
    not pull Stage 5 validation into Stage 4.
14. **Run the complete Stage 4 verification.**

## Verification

Stage 4 verification must cover:

- supported, ambiguous, and impossible dates;
- valid, invalid, missing, and lower-case currencies;
- decimal and grouping separator rules;
- negative and malformed amounts;
- quantity parsing;
- text and identifier preservation;
- `null` and empty values;
- line items;
- structured error recording;
- database relationships and constraints;
- lifecycle and retries;
- concurrency protection;
- transaction rollback;
- retrieval and `404`/`409` behavior;
- preservation of every Stage 3 raw value and the original PDF;
- proof that no AI or external-network call occurs.
