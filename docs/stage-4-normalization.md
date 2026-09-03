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

## Normalization policies (decided — step 2)

These policies are fixed. The step 6–10 normalizers implement exactly what is
written here and must not add rules or guess beyond it. All processing is
deterministic, in-process, and offline.

### Dates (`invoice_date`, `due_date`)

Input is the raw extracted string. Before parsing: trim, collapse internal
whitespace, drop a single trailing period, and remove an ordinal suffix on the
day (`1st`, `2nd`, `3rd`, `15th`, … case-insensitive).

Accepted input formats (English invoices):

| Family | Examples | Interpreted as |
|---|---|---|
| Year-first, 4-digit year | `2026-01-15`, `2026/1/5`, `2026.01.15` | `YYYY-(M)M-(D)D` |
| Day, English month name, 4-digit year | `15 Jan 2026`, `15 January 2026`, `15-Jan-2026` | D MON YYYY |
| English month name, day, 4-digit year | `Jan 15, 2026`, `January 15 2026` | MON D YYYY |

Month names are full or three-letter English, case-insensitive. One- or
two-digit day and month are accepted in the year-first family and zero-padded
on output.

All-numeric dates with separators `/`, `-`, or `.` and a leading component of
one or two digits (`03/04/2026`, `4-3-2026`):

- If exactly one of the first two components is greater than 12, that component
  is the day and the order is resolved (`13/04/2026` → 13 April 2026).
- If both are 12 or less, the day/month order is not fixed by the value. It is
  read using the **default field order, day-first (`DD/MM/YYYY`)**:
  `03/04/2026` → 3 April 2026. This default is fixed Stage 4 policy and is
  applied without recording an error. (A configurable per-source order may be
  added later; until then every source is treated as day-first.)
- If the day-first reading is not a real calendar date → `invalid_date`,
  normalized value `null`.

Rejected → `invalid_date`, normalized value `null`:

- Two-digit years (`15/01/26`) — the century is not guessed.
- Any format not listed above (localized or non-English month names, weekday
  names, quarter/week notation, epoch numbers, `YYYYMMDD` with no separators).
- Values that parse structurally but are not real calendar dates
  (`2026-02-30`, `2026-13-01`, `0000-00-00`).

No plausibility window is applied — a far-past or far-future but real date
normalizes successfully; judging it belongs to Stage 5.

Output: `YYYY-MM-DD`, zero-padded, proleptic Gregorian.

### Numbers — money and quantities

Fields: `subtotal`, `tax_amount`, `total_amount`, and line-item `quantity`,
`unit_price`, `line_total`.

By the time normalization runs these values have already been parsed into
`Decimal` by the Stage 3 extraction contract
(`app.schemas.extraction.Money` / `Quantity`), so normalization does **not**
re-parse invoice number text in the current pipeline. Its number rules are:

- Accept any finite `Decimal`. NaN and infinity are already impossible; if one
  is somehow seen → `invalid_number`.
- **Sign is preserved.** Negative amounts and quantities are valid (credits,
  adjustments, returns) and pass through unchanged.
- **No rounding and no quantization.** The exact value and scale from
  extraction are kept, including trailing zeros. Aligning money to a currency's
  minor unit is a later-stage concern.
- A `None` value normalizes to `null` with no error.

The following **string** parsing rules are the fixed policy for any numeric
value that reaches normalization as text (a future provider, or a raw-response
path). They are decided now so the behavior is not invented later:

- Decimal separator: `.` (period) only.
- Grouping separator: `,` (comma) or a space (incl. `U+00A0`, `U+202F`),
  permitted only between digits in groups of exactly three.
  `1,234,567.89` → `1234567.89`.
- Leading/trailing whitespace and an embedded currency symbol or 3-letter code
  are stripped before parsing (`$1,234.56`, `USD 1 234.56` → `1234.56`).
- Accounting negatives: surrounding parentheses (`(123.45)`) and a trailing
  minus (`123.45-`) both mean a negative value; a leading `+` is dropped.
- A value using a decimal comma (`1234,56`, `1.234,56`) is **not** assumed to
  be European — it is reported as `ambiguous_number`, normalized value `null`.
- Anything not resolvable to a single finite decimal by these rules →
  `invalid_number`, normalized value `null`.

### Currency (`currency`)

The extraction contract already guarantees this field is `null` or a
three-letter alphabetic string, upper-cased. Normalization then:

- Trims and upper-cases defensively (a no-op in practice).
- Accepts the code only if it is on the approved list: the **current ISO 4217
  alphabetic codes for circulating national currencies**, held as a vendored
  in-repo constant (no network access). Multi-country codes such as `EUR`,
  `XCD`, `XOF`, `XAF`, and `XPF` are included.
- Rejects as `unknown_currency` (normalized value `null`) any well-formed code
  not on the list: obsolete/withdrawn codes (`DEM`, `FRF`, `ITL`), the “no
  currency” and testing codes (`XXX`, `XTS`), and precious-metal or
  supranational codes (`XAU`, `XAG`, `XPT`, `XPD`, `XDR`, `XSU`, `XUA`,
  `XBA`–`XBD`).
- Reports a value that is not three alphabetic characters as
  `invalid_currency` (should not occur given the contract, but the rule is
  defined).

A missing currency stays `null`. It is never defaulted and never inferred.

### Currency-symbol interpretation

There is **none** in Stage 4.

- The `currency` field arrives from extraction as a code or `null`; a bare
  symbol (`$`, `€`, `£`) cannot appear there (the contract forbids non-alpha).
- A symbol found inside an amount string is stripped for parsing only and then
  discarded; it never populates a missing `currency`.
- Inferring currency from a symbol is ambiguous (`$` = USD/CAD/AUD/MXN/…) and
  would be a form of defaulting, which Stage 4 forbids. Any currency-region
  inference belongs to a later stage.

### Text trimming and whitespace cleanup

Fields: `invoice_number`, `vendor_name`, `vendor_tax_id`, `customer_name`,
line-item `description`. Applied in this order:

1. Apply Unicode normalization form **NFC**.
2. Replace every Unicode whitespace character (tab, newline, no-break space
   `U+00A0`, narrow no-break space `U+202F`, figure space `U+2007`, …) with a
   plain space `U+0020`.
3. Remove zero-width and BOM characters (`U+200B`–`U+200D`, `U+FEFF`) and
   C0/C1 control characters.
4. Collapse runs of spaces to one.
5. Trim leading and trailing spaces.

Not done: case changes, punctuation removal, accent/diacritic folding,
transliteration, quote or dash normalization. `Café Müller & Co. (Ltd.)`
survives verbatim except for whitespace. `description` becomes single-line
(newlines became spaces in step 2).

`invoice_number` and `vendor_tax_id` get the same whitespace cleanup and
nothing else — internal spaces and separators (`INV 2026 / 0007`,
`DE 123 456 789`) carry meaning and are not de-spaced, upper-cased, or
reformatted.

### Empty strings

After the cleanup above, an empty result (`""`) — including a source value that
was already `null`, `""`, or whitespace-only — normalizes to `null` with **no
error**. An absent value is not a normalization failure.

### Maximum normalized text lengths

Measured in Unicode characters after cleanup:

| Field | Limit |
|---|---|
| `invoice_number` | 100 |
| `vendor_tax_id` | 60 |
| `vendor_name`, `customer_name` | 256 |
| line-item `description` | 512 |

Over the limit → `text_too_long`, normalized value `null`. The value is
**never silently truncated** — truncating an identifier or a party name
corrupts data. The limits are generous; hitting one indicates bad extraction
rather than a real invoice value.

### Behavior when normalization fails

Two separate kinds of failure:

1. **Field-level normalization error** — the value is present but invalid or
   ambiguous under the rules above. This is *data about the invoice*, not a
   technical failure. The normalizer sets that field's normalized value to
   `null`, appends one `NormalizationError` (`field_path`, `raw_value` = the
   source value stringified, `code`, safe `message`), and continues with every
   other field and line item. The attempt still ends **`COMPLETED`**, with a
   non-empty `errors` list.
2. **Technical failure** — the source extraction cannot be loaded or is not
   `COMPLETED`, a database write fails, or the normalizer engine raises an
   unexpected exception. The attempt ends **`FAILED`** with a safe
   `failure_code` / `failure_message`; no partial normalized result is
   persisted (the write is transactional); the source extraction and PDF are
   untouched; an explicit retry is allowed.

Closed set of field-error codes (this finalizes `NormalizationErrorCode` from
step 1 at six members):

| Code | Meaning |
|---|---|
| `invalid_date` | Not a real calendar date in an accepted format |
| `invalid_currency` | Not a three-letter alphabetic token |
| `unknown_currency` | Well-formed but not on the approved ISO 4217 list |
| `invalid_number` | Not resolvable to a single finite decimal |
| `ambiguous_number` | Decimal/grouping separators parseable more than one way |
| `text_too_long` | Exceeds the field's maximum length |

A numeric date with an undetermined day/month order is **not** in this set — it
is resolved day-first by default (see "Dates" above), so there is no
`ambiguous_date` code.

Messages are fixed, generic, and client-safe — no paths, secrets, stack
traces, or raw payloads — e.g. *“The invoice date could not be recognized in a
supported format.”*

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

*(Done — step 3, `backend/app/models/normalization.py`, test
`test_normalization_model.py`.)* Three tables mirror the Stage 3 extraction
layout (`invoice_extractions` / `invoice_line_items`):

**`invoice_normalizations`** — one row per normalization attempt.

| Column | Type / rule |
|---|---|
| `normalization_id` | UUID PK |
| `extraction_id` | FK → `invoice_extractions.extraction_id`, `ON DELETE CASCADE`; never written back to |
| `attempt_number` | int ≥ 1; unique with `extraction_id` |
| `status` | native enum `normalization_status` (`PROCESSING` / `COMPLETED` / `FAILED`) |
| `started_at`, `completed_at` | timestamptz; `completed_at` null iff `PROCESSING` |
| `failure_code` (≤64), `failure_message` | both set **only** when `status = FAILED` (technical failure) |
| `created_at`, `updated_at` | timestamptz |
| scalar fields | `invoice_number` varchar(100), `invoice_date`/`due_date` varchar(10), `vendor_name`/`customer_name` varchar(256), `vendor_tax_id` varchar(60), `currency` varchar(3), `subtotal`/`tax_amount`/`total_amount` numeric — all nullable, **no confidence column** |

CHECK constraints: `status_fields_consistent` (as Stage 3), `attempt_number >= 1`,
`invoice_date` / `due_date` match `^\d{4}-\d{2}-\d{2}$` or null, `currency`
matches `^[A-Z]{3}$` or null. Partial unique index
`… one_active_per_extraction` on `extraction_id WHERE status = 'PROCESSING'`.

**`invoice_normalized_line_items`** — `normalized_line_item_id` UUID PK;
`normalization_id` FK (`CASCADE`); `position` int ≥ 0, unique with
`normalization_id`; `description` varchar(512), `quantity` / `unit_price` /
`line_total` numeric, all nullable; `created_at`. Order mirrors the source
extraction's line items 1:1.

**`invoice_normalization_errors`** — `normalization_error_id` UUID PK;
`normalization_id` FK (`CASCADE`); `field_path` varchar(64) restricted to the
ten scalar fields or a canonical `line_items.<index>.<line-item-field>` path,
unique with `normalization_id` (one error per field path); `raw_value` text nullable
(null only when the source value was null); `code` native enum
`normalization_error_code` (the six members); `message` text (client-safe);
`created_at`.

`NormalizationErrorCode` is defined by `app.schemas.normalization` and reused
by the persistence model. PostgreSQL stores its specified lower-case values,
so the pure contract does not depend on the ORM and the two layers cannot drift
into different closed sets. `ExtractionAttempt` gains a
`normalizations` relationship (cascade delete, oldest attempt first); nothing
in Stage 3 behaviour changes.

Migration: `alembic/versions/0003_normalization_tables.py` (down_revision
`0002_invoice_extraction_tables`) creates the two enum types and three tables
with every constraint and the partial unique index, and drops them (enum types
included) on downgrade. Verified end to end on a throwaway database: seed a
Stage 2 document + Stage 3 extraction at `0002`, upgrade to `head`, confirm the
Stage 2/3 rows are untouched, the one-active-attempt index, the
`status_fields_consistent` / `currency` / `field_path` CHECKs, and the
document-delete cascade all work; downgrade to `0002` and confirm the three
tables and both enum types are gone while `documents` / `invoice_extractions`
and their enums remain; re-upgrade to `head`; `alembic check` reports no drift
from the models.

Other requirements still in force:

- Stage 3 rows stay immutable — normalization code only reads them.
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

1. **Define the normalization contract.** *(Done —
   `backend/app/schemas/normalization.py`, test
   `test_normalization_contract.py`.)* `NormalizedInvoice` holds the ten scalar
   fields (each a single canonical value or `null`, no confidence),
   `line_items` as `NormalizedLineItem` entries in source order, and `errors`
   as a flat list of `NormalizationError`. `NormalizedInvoiceResult` binds the
   contract to its `source_extraction_id`. Decision recorded: each error
   records `field_path` (a stable path such as `total_amount` or
   `line_items.0.unit_price`), `raw_value` (the offending source value,
   stringified, or `null`), `code` (a member of the closed
   `NormalizationErrorCode` set — starter set only; step 2 finalizes it), and a
   client-safe `message`. An error path uses canonical zero-based indexes and
   the normalized value at that path must be `null`. Canonical leaf types:
   `NormalizedDate` (`YYYY-MM-DD`, real calendar date),
   `NormalizedCurrencyCode` (3 letters,
   upper-cased; ISO 4217 list check deferred to step 8), `NormalizedText`
   (trimmed, non-empty — an empty result must be `null`). Schema definition
   only; no AI, no database.
2. **Document normalization policies.** *(Done — see "Normalization policies
   (decided — step 2)" above.)* Dates, numbers, currency, currency symbols,
   whitespace, empty values, text limits, and failure representation are all
   fixed there, and `NormalizationErrorCode` is finalized at six members.
   Undetermined-order numeric dates are read day-first (`DD/MM/YYYY`) by
   default rather than erroring.
3. **Design normalization persistence models.** *(Done — see "Normalization
   persistence" above.)* Three tables: `invoice_normalizations`,
   `invoice_normalized_line_items`, `invoice_normalization_errors`. Attempt
   history via `(extraction_id, attempt_number)`; one active attempt via a
   partial unique index; Stage 3 rows untouched.
4. **Create and verify the Alembic migration.** *(Done — see "Normalization
   persistence" above.)* `0003_normalization_tables` creates the two enum types
   and three tables with all foreign keys, constraints and the partial unique
   index; downgrade drops them cleanly. Verified up / down / re-up on a
   throwaway database with seeded Stage 2/3 rows; `alembic check` reports no
   drift.
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
