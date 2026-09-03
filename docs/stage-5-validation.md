# Stage 5: deterministic invoice validation — specification

This is the detailed specification for Stage 5. `CLAUDE.md` carries the
high-level summary and the guardrails; this file holds the boundary (step 1),
the pinned validation policies (step 2), the validation contract and rule
catalogue these imply, the implementation order, and the verification list.

**Status.** Steps 1 and 2 (this document) are done. Steps 3–14 are **not
authorized** — do not start any of them, or write engine / schema / migration /
API code, until the user explicitly authorizes that step. Nothing in the
codebase depends on the policies below yet; they live only here until step 8.

**Policy values marked ⚠ are judgement calls** made to unblock the design.
They are deliberately conservative and self-documenting (every
threshold-based finding records the threshold it used). Expect the reviewer to
confirm or retune them against real invoice data; changing a ⚠ value is a doc
edit until step 8.

Stage 5 converts a completed Stage 4 normalization into a separate, traceable
set of **structured findings**. It makes no AI call and no external-network
call, and it uses `Decimal` arithmetic for every numeric comparison.

---

## Part 1 — Validation boundary (step 1)

### 1.1 What Stage 5 is

Given one `COMPLETED` normalization attempt, Stage 5 evaluates a fixed catalogue
of deterministic rules and records, for each rule that fires, a structured
**finding**: what rule, how serious, which field, the expected and actual values
where it is safe to show them, and a client-safe explanatory sentence. The
finding list is the product. Stage 5 asserts facts; it does not judge the
invoice.

### 1.2 Inputs

Stage 5 reads, and never writes:

| Input | Source | Notes |
|---|---|---|
| The normalization attempt | `invoice_normalizations` row by `normalization_id` | Must be `status = COMPLETED`. Carries the ten canonical scalar values. |
| Its normalized line items | `invoice_normalized_line_items` | Ordered by `position`. |
| Its normalization errors | `invoice_normalization_errors` | The six Stage 4 codes; re-surfaced as findings, never re-evaluated. |
| Per-field extraction confidence | the source `invoice_extractions` row via `normalization.extraction_id` | `<field>_confidence` columns, `Decimal` in `[0,1]` or `null`. |
| Duplicate candidates | the latest `COMPLETED` normalization of every **other** document | See rule `probable_duplicate_invoice` for the candidate query and key. |
| Validation policy constants | a vendored `policy` module (step 8) | Not environment config — see §2.9. |

Stage 5 does **not** read the original PDF, the raw provider payload, OCR text,
or any extraction value other than confidence. It re-uses the normalized
canonical values as the single source of invoice data.

### 1.3 Outputs

One **validation attempt** with:

- `status` — `PROCESSING | COMPLETED | FAILED` only. No pass/fail verdict.
- `findings` — a list of structured findings (may be empty). Each finding
  carries: `rule` (a stable code from the closed catalogue in §3), `severity`
  (`error | warning | info`, defined in §1.6), `field_path` (a Stage 4 field
  path — a scalar name or `line_items.<i>.<field>` — or `null` for an
  invoice-level finding), `expected` / `actual` (client-safe scalar strings or
  decimals, or `null`), `message` (a fixed, generic, client-safe sentence),
  and `context` (a small structured object for rule-specific facts, e.g. the
  threshold used or the matched document id).
- `summary` — descriptive counts only: total findings and a count per severity.
  This is a convenience view of the list, **not** a decision.
- `failure_code` / `failure_message` — set only when `status = FAILED`
  (technical failure), client-safe, mirroring Stage 3/4.

### 1.4 What Stage 5 must NOT do

- No acceptance, rejection, approval, "valid/invalid" verdict, or score.
- No escalation, no `NEEDS_REVIEW`, no document-status change of any kind.
  Stage 5 never touches the `documents` row.
- No mutation of any Stage 2–4 record (documents, extractions, line items,
  normalizations, normalized line items, normalization errors, stored files).
- No confidence *filtering* — Stage 5 flags a low-confidence field, it never
  discards or blanks the value.
- No currency conversion, FX lookup, or defaulting.
- No re-running of extraction or normalization, and no re-parsing of a raw
  value that Stage 4 already ruled on.
- No AI call, no network call, no non-deterministic input beyond the
  validation run timestamp (see §2.8).

The later **decision / escalation stage** is what consumes Stage 5 findings
(together with confidence and duplicate data) to decide accept / review /
reject and to move a document to `NEEDS_REVIEW`. That stage is out of scope
here and is not designed in this document.

### 1.5 Lifecycle position

```
COMPLETED normalization ──(start)──> PROCESSING ──> COMPLETED   (with 0..n findings)
                                              └────> FAILED      (technical only)
FAILED validation ──(explicit retry)──> PROCESSING ──> COMPLETED | FAILED
```

- Validation can start only from a `COMPLETED` normalization attempt. A
  `PROCESSING` or `FAILED` normalization → `409`.
- At most one active (`PROCESSING`) validation attempt per normalization
  attempt; concurrent or illegal starts → `409`.
- A `COMPLETED` validation attempt is terminal and is not re-run. Stage 4 only
  permits retry after a technical normalization failure, so the current MVP has
  no route to re-validate completed normalized data. A future explicit
  "re-validate" capability is deferred; note that duplicate and
  date-plausibility findings are as-of the run (see §2.8).
- Attempt history is preserved: `attempt_number` 1, 2, 3, … per normalization
  attempt; a retry never mutates an earlier attempt.
- A rule violation is **not** a `FAILED` attempt. `FAILED` means only: the
  normalization attempt could not be read, a database write failed, or a rule
  function raised an unexpected exception. A `FAILED` attempt persists no
  partial findings and leaves every source record intact.

### 1.6 Severity scale

Severity describes *how much a human should care*, not a decision:

- **`error`** — the invoice is missing something required to process it at all,
  or a required field failed normalization. (Rules: `missing_required_field`;
  `normalization_error` on a required field.)
- **`warning`** — a probable data problem or inconsistency a reviewer should
  look at: totals or line items that do not reconcile, an out-of-order or
  implausible date, a low-confidence critical field, a probable duplicate,
  a normalization error on a non-required field.
- **`info`** — a fact worth recording for the decision stage that is not itself
  a problem: a high-value invoice, no line items, a check skipped because an
  input was absent, confidence unavailable for a critical field.

Stage 5 never collapses severities into an outcome. `summary` reports the
counts; the decision stage applies its own thresholds later.

### 1.7 Boundary tests (part of step 1)

Contract-level tests that fail if the boundary is crossed:

- the validation status enum contains exactly `PROCESSING`, `COMPLETED`,
  `FAILED` — no `ACCEPTED` / `REJECTED` / `NEEDS_REVIEW` / `ESCALATED`;
- the finding model has no field expressing a decision (no `accepted`,
  `action`, `disposition`, `resolution`, …); its `severity` enum is exactly
  `error | warning | info`;
- `summary` exposes counts only, no boolean verdict;
- a Stage 5 run leaves every `documents` / `invoice_extractions` /
  `invoice_normalizations*` row byte-for-byte unchanged (asserted end to end).

---

## Part 2 — Validation policies (step 2)

### 2.1 Required fields

An invoice must carry these five fields for the business to process it — the
same set as `CRITICAL_FIELDS` in `app/schemas/extraction.py`:

```
invoice_number
invoice_date
vendor_name
currency
total_amount
```

`due_date`, `vendor_tax_id`, `customer_name`, `subtotal`, `tax_amount`, and the
line items are **not required**; rules that need them simply do not run when
they are absent (and say so with an `info` finding where useful).

A required field is **missing** when its normalized value is `null` — whether
the source value was absent or Stage 4 could not normalize it. Each missing
required field produces one `missing_required_field` finding at that field
path, severity `error`. (If the field is `null` *because* of a Stage 4
normalization error, both `missing_required_field` and `normalization_error`
fire; that is intentional — one states the field is unusable, the other says
why.)

### 2.2 Date rules

All dates are canonical `YYYY-MM-DD` or `null` (Stage 4 guarantees real
calendar dates; an unparseable date is already `null` + a normalization error).

| Rule | Trigger | Severity |
|---|---|---|
| `due_date_before_invoice_date` | both present and `due_date < invoice_date` | `warning` |
| `due_date_far_after_invoice_date` | both present and `due_date > invoice_date + 365 days` ⚠ | `warning` |
| `invoice_date_in_future` | `invoice_date` present and `invoice_date > run date` (§2.8) | `warning` |
| `invoice_date_implausibly_old` | `invoice_date` present and `invoice_date < run date − 10 years` ⚠ | `warning` |

No rule *rejects* a date; Stage 4 already dropped the ones that are not real
calendar dates. `due_date_before_invoice_date` is a `warning` (not `error`)
because it is occasionally legitimate (prepaid, credit note).

### 2.3 Monetary reconciliation tolerance

Rule `totals_do_not_reconcile`. Runs only when `subtotal`, `tax_amount`, and
`total_amount` are all non-null.

```
reconciles  ⇔  abs((subtotal + tax_amount) − total_amount) ≤ 0.01      ⚠
```

- Tolerance is an **absolute 0.01** in the invoice's own currency — enough to
  absorb one minor-unit rounding in the source document, tight enough that a
  real discrepancy is caught. (Stage 4 does not quantise to minor units, so a
  per-currency minor unit is not available; a flat 0.01 is the pragmatic
  choice. This also permits a difference up to `0.01` for JPY and other
  zero-decimal currencies; exact per-currency minor-unit handling is deferred
  unless the reviewer chooses it in the open questions.)
- Sign is not special-cased: the equation holds for negative totals
  (credit notes) unchanged.
- Severity `warning`, not `error`: a mismatch can mean an uncaptured discount
  or shipping line rather than bad data. `expected` = `subtotal + tax_amount`,
  `actual` = `total_amount`, `context.delta` = the signed difference.
- If any of the three is `null` the rule does not run (no finding). If
  `total_amount` is the `null` one, `missing_required_field` already fired.

### 2.4 Line-item reconciliation rules

**`line_item_amount_mismatch`** — per line item, runs only when that item's
`quantity`, `unit_price`, and `line_total` are all non-null:

```
matches  ⇔  abs(quantity × unit_price − line_total) ≤ 0.01               ⚠
```

Severity `warning`, `field_path = line_items.<i>.line_total`, with
`expected` / `actual` / `context.delta`. A line item missing any of the three
values is skipped silently.

**`line_items_do_not_sum`** — runs only when `line_items` is non-empty **and
every** line item has a non-null `line_total` (a partial sum is meaningless):

```
target  = subtotal                       if subtotal is non-null
        = total_amount − tax_amount       else if both non-null
        = total_amount                    else if total_amount is non-null
        = (rule does not run)             otherwise

sums  ⇔  abs(Σ line_total − target) ≤ max(0.01, 0.01 × line_count)        ⚠
```

Severity `warning`, `field_path = null` (invoice-level), `context.target_basis`
records `subtotal`, `total_less_tax`, or `total` to identify the selected
target, together with `context.line_count`,
`context.sum`, `context.delta`. The tolerance grows by one minor unit per line
to absorb accumulated per-line rounding.

If some line items lack `line_total`, the rule does not run and an `info`
finding `line_item_sum_not_checked` is recorded (with how many items lacked a
total), so the decision stage knows the check was skipped rather than passed.

### 2.5 Duplicate matching criteria

Rule `probable_duplicate_invoice`. Deterministic, **exact-match only** — no
fuzzy / edit-distance matching in the MVP (noted as a future extension).

**Candidate set.** The latest `COMPLETED` normalization belonging to the latest
`COMPLETED` extraction of every document *other than* the one under validation
(join `invoice_normalizations → invoice_extractions → documents`, exclude the
current `document_id`; first choose the highest completed extraction
`attempt_number` per document, then its highest completed normalization
`attempt_number`, if one exists). Do not fall back to an older extraction when
the latest one has no completed normalization. A candidate must have
`normalization.completed_at <= validation.started_at`, so a normalization that
finishes concurrently after validation started cannot leak into the run's
as-of snapshot. See §2.8 on A/B asymmetry.

**Match key** — a candidate is a probable duplicate of the invoice under
validation when **all** of these hold (both sides non-null and equal):

1. vendor identity: equal `vendor_tax_id` **or**, when either side's
   `vendor_tax_id` is null, equal `vendor_name` (exact string; Stage 4 already
   applied NFC + whitespace normalisation and does not case-fold);
2. equal `invoice_number` (exact string — an invoice number is unique per
   vendor, not globally, so vendor identity is required alongside it);
3. equal `currency`;
4. `abs(total_amount − candidate.total_amount) ≤ 0.01` (the §2.3 tolerance).

If `invoice_number`, `vendor_name`/`vendor_tax_id`, `currency`, or
`total_amount` is null on the invoice under validation, the rule does not run
(the missing-required rule covers the null critical fields).

**Output.** One finding regardless of how many candidates match, severity
`warning`, `field_path = null`, `context.matches` = a list of
`{ document_id, normalization_id }` for the matched candidates, sorted by
`document_id` and then `normalization_id` for reproducible output. Exposing a
sibling `document_id` is a business fact, not an internal diagnostic; ⚠ the
reviewer should confirm this is acceptable before there is multi-tenancy.

### 2.6 Confidence thresholds

Evaluated for the five required/critical fields only, using the per-field
confidence from the source extraction (`<field>_confidence`).

```
critical_field_confidence_min = 0.70        ⚠
```

Per critical field whose **value is present**:

| Confidence | Finding | Severity |
|---|---|---|
| `≥ 0.70` | none | — |
| `< 0.70` | `low_confidence_critical_field` (`context.confidence` = actual) | `warning` |
| `null` | `critical_field_confidence_unavailable` | `info` |

`null` confidence is **never** treated as high or low. It produces its own
`info` finding so the decision stage knows the check could not be made — this
is the resolution of step 5, and it means the rule always runs and always
produces a consistent result regardless of provider. (The current OpenAI
adapter reports `null` for every field, so on OpenAI-extracted invoices this
rule yields five `info` findings and no `warning`; the deterministic fake
provider carries real confidences and exercises the `warning` path.)

A critical field that is *absent* gets `missing_required_field` only — no
confidence finding is added for a field with no value.

Non-critical field confidence is not evaluated in the MVP.

### 2.7 High-value thresholds

Rule `high_value_invoice`. Runs when `total_amount` and `currency` are both
non-null.

```
flagged  ⇔  abs(total_amount) ≥ threshold(currency)
```

`threshold(currency)` comes from a small vendored map with a default;
**magnitudes below are placeholders** ⚠ — set them from the real invoice
distribution:

| Currency | Threshold (major units) |
|---|---|
| EUR, USD, GBP, CHF | 10 000 |
| JPY | 1 500 000 |
| *(any other approved code)* | 10 000 (default) |

- `abs(total_amount)` so a large credit note is flagged for the same
  attention as a large charge.
- Severity `info` — a high value is not a data defect, it is a business-
  attention flag for the decision stage. `context.threshold` and
  `context.currency` are recorded so the finding explains itself even if the
  map later changes.
- If `currency` is null the rule does not run (`missing_required_field`
  covers it).

### 2.8 Determinism and "as-of" findings

Every rule is a pure function of its inputs plus **one** external value: the
validation attempt's own start date (`started_at`, as a UTC calendar date).
Rules `invoice_date_in_future` and `invoice_date_implausibly_old` use it.

Duplicate detection additionally depends on which other documents are already
normalized when the attempt runs. Consequences, all acceptable and to be
documented in the API:

- retry creates a new attempt only after a technical `FAILED`; a `COMPLETED`
  validation is frozen with its findings and run date, and cannot currently be
  re-run because Stage 4 does not re-normalize a completed result;
- if document A is validated before document B exists, A carries no duplicate
  finding and B (validated later) flags A — B is the later, probable-duplicate
  copy, which is the desired direction.

Given identical inputs and the same `started_at` date, a rule run is fully
reproducible.

### 2.9 Cross-cutting policy

- **Numeric comparisons** use `Decimal` throughout, in a precision context wide
  enough that no operand or intermediate product is rounded; equality is
  `abs(a − b) ≤ tolerance`. No binary floating point anywhere.
- **Negative amounts** (credit notes, adjustments) are valid and flow through
  every rule; no rule flags a value solely for being negative.
- **Currency** is used only to pick a high-value threshold and as part of the
  duplicate key. Amounts in different currencies are never added or compared
  across currencies, and never converted.
- **Policy constants** live in one vendored module
  (`app/services/processing/validation/policy.py`, added at step 8), not in
  environment configuration, so a validation run is reproducible from the code
  alone. Findings record the constant they used. Promoting a specific
  threshold to runtime configuration is a later, separate decision.
- **Field paths** in findings reuse the Stage 4 vocabulary exactly: the ten
  scalar names, `line_items.<index>.<field>` with a zero-based index, or `null`
  for an invoice-level finding.
- **Messages** are fixed, generic, and client-safe — no paths, secrets, stack
  traces, raw payloads, or PII beyond what the finding's `expected` / `actual`
  legitimately show (which are the invoice's own normalized values).

---

## Part 3 — Rule catalogue (preview; formalised in step 4)

| Code | Trigger (all inputs non-null unless noted) | `field_path` | Severity | Skips when |
|---|---|---|---|---|
| `missing_required_field` | a required field (§2.1) is `null` | that field | `error` | — |
| `normalization_error` | a Stage 4 error exists for a field | that field | `error` if required field, else `warning` | — |
| `due_date_before_invoice_date` | `due_date < invoice_date` | `due_date` | `warning` | either date `null` |
| `due_date_far_after_invoice_date` | `due_date > invoice_date + 365d` ⚠ | `due_date` | `warning` | either date `null` |
| `invoice_date_in_future` | `invoice_date > run date` | `invoice_date` | `warning` | `invoice_date` `null` |
| `invoice_date_implausibly_old` | `invoice_date < run date − 10y` ⚠ | `invoice_date` | `warning` | `invoice_date` `null` |
| `totals_do_not_reconcile` | `abs(subtotal + tax_amount − total_amount) > 0.01` ⚠ | `null` | `warning` | any of the three `null` |
| `line_item_amount_mismatch` | `abs(quantity × unit_price − line_total) > 0.01` ⚠ | `line_items.<i>.line_total` | `warning` | any of the three `null` on that item |
| `line_items_do_not_sum` | `abs(Σ line_total − target) > max(0.01, 0.01 × n)` ⚠ | `null` | `warning` | no line items, or any `line_total` `null`, or no target |
| `line_item_sum_not_checked` | line items exist but ≥1 `line_total` is `null` | `null` | `info` | — |
| `low_confidence_critical_field` | critical field value present, confidence `< 0.70` ⚠ | that field | `warning` | confidence `null`, or value absent |
| `critical_field_confidence_unavailable` | critical field value present, confidence `null` | that field | `info` | value absent |
| `probable_duplicate_invoice` | match key in §2.5 satisfied by ≥1 candidate | `null` | `warning` | any key field `null` |
| `high_value_invoice` | `abs(total_amount) ≥ threshold(currency)` ⚠ | `null` | `info` | `total_amount` or `currency` `null` |
| `no_line_items` | `line_items` is empty | `null` | `info` | — |

The catalogue is **closed**: the finding `rule` field is an enum over exactly
these codes (mirroring how `NormalizationErrorCode` is closed).

---

## Implementation order

Introduce no AI or external-network call at any step.

1. **Define the validation boundary.** *(Done — Part 1 above, plus the boundary
   tests in §1.7 to be written alongside the contract in step 3.)*
2. **Set validation policies.** *(Done — Part 2 above. ⚠ values are provisional
   defaults pending review against real invoices; they are documented here and
   in no code.)*
3. **Design the validation contract.** `app/schemas/validation.py`:
   `ValidationStatus` (`PROCESSING|COMPLETED|FAILED`), `FindingSeverity`
   (`error|warning|info`), `ValidationRule` (closed enum, §3), `ValidationFinding`
   (`rule`, `severity`, `field_path`, `expected`, `actual`, `message`,
   `context`), `InvoiceValidation` (`findings`, `summary`), and a result type
   binding it to its source `normalization_id`. Decimals serialise as strings;
   unknown keys rejected; the boundary tests from §1.7.
4. **Define the rule catalogue.** Formalise §3 as `ValidationRule` members and,
   for each, a spec of inputs, skip conditions, severity, and message text.
5. **Resolve confidence limitations.** *(Decided — §2.6: `null` confidence →
   `critical_field_confidence_unavailable` (`info`), never treated as a value.)*
   Implement the join to `invoice_extractions.<field>_confidence`.
6. **Add persistence.** `invoice_validations` (one row per attempt, keyed
   `(normalization_id, attempt_number)`, partial unique index for one active
   attempt, status/failure CHECKs mirroring Stage 4) and
   `invoice_validation_findings` (`rule`, `severity`, `field_path`, `expected`,
   `actual`, `message`, `context` JSONB; `field_path` CHECK reusing the Stage 4
   shape or `null`). `NormalizationAttempt` gains a `validations` relationship,
   cascade-on-document-delete. No Stage 4 behaviour changes.
7. **Add the database migration.** `0004_validation_tables` (down_revision
   `0003_normalization_tables`); verify up / down / re-up on a throwaway
   database with seeded Stage 2–4 rows intact; `alembic check` clean.
8. **Implement deterministic rule functions.** One module of pure functions
   plus `policy.py` (the §2 constants and the high-value map). Each rule takes
   the normalized invoice (+ confidence, + candidates, + run date where needed)
   and returns zero or more findings. `Decimal` only; independently unit-tested.
9. **Build the validation engine.** Load the normalization attempt, its errors,
   the confidence row, and the duplicate candidates; run every applicable rule;
   assemble and re-validate the `InvoiceValidation`.
10. **Implement lifecycle and retry behaviour.** `lifecycle.py` mirroring Stage
    4: `ensure_normalization_can_validate` (source must be `COMPLETED`;
    `NORMALIZATION_NOT_COMPLETED` / `VALIDATION_IN_PROGRESS` /
    `NORMALIZATION_ALREADY_VALIDATED` / `VALIDATION_FAILED` /
    `VALIDATION_NOT_FAILED`), and `ensure_attempt_transition`. Rule violations →
    `COMPLETED` with findings; only technical faults → `FAILED`.
11. **Add service and repository layers.** `ValidationRepository` (sole
    reader/writer of the two tables) and `ValidationService.start` / `retry`:
    lock the source normalization row, commit `PROCESSING` before the engine
    runs, persist findings + mark `COMPLETED` in one transaction, roll back to a
    generic `VALIDATION_FAILED` on any fault, preserve history, never touch a
    Stage 2–4 row.
12. **Add validation endpoints.** Under
    `/documents/{id}/extractions/{eid}/normalizations/{nid}/validations`:
    `POST` (start), `POST …/retry`, `GET` (history, newest first),
    `GET …/latest`, `GET …/{vid}`. Response `InvoiceValidationResult`
    (`status`, timestamps, `failure_*`, `data: InvoiceValidation`); empty body,
    `422` on unknown keys; `404` `…_NOT_FOUND`; `409` the §1.5 lifecycle codes;
    a technical failure is still `201` with `status = FAILED`.
13. **Extend the processing pipeline.** `ProcessingPipeline.run` gains a third
    stage: after a `COMPLETED` normalization, run validation; `PipelineRunResult`
    gains `validation: InvoiceValidationResult | null` (`null` when normalization
    did not complete). Each stage stays independently callable; no rule logic
    moves into the pipeline.
14. **Add verification tests and documentation.** The list below, a
    `test_stage5_verification.py` bullet-to-test map, and README + CLAUDE.md
    updates.

---

## Verification

Stage 5 verification must cover:

- every rule in the catalogue on passing and failing inputs, including the
  tolerance edges (exactly at, just over) for each ⚠ threshold;
- `missing_required_field` for each required field, and its co-occurrence with
  `normalization_error` when the field is `null` because of a Stage 4 error;
- normalization errors surfaced as findings without re-running normalization,
  with severity keyed to whether the field is required;
- date order, future, and implausibly-old findings, and the `run date`
  dependence (§2.8);
- monetary and line-item reconciliation, including the `subtotal` →
  `total_amount − tax_amount` → `total_amount` target precedence and the
  per-line tolerance growth;
- `line_item_sum_not_checked` / `no_line_items` info findings;
- confidence: `warning` below the threshold, `info` on `null`, nothing at or
  above, nothing for an absent field — on both the fake provider (real
  confidences) and an all-`null` confidence row (OpenAI-like);
- duplicate detection: exact match, each key field differing in turn, the
  candidate-set scoping (other documents only, latest completed extraction and
  normalization, `completed_at` cutoff), deterministic match ordering, the
  multi-match single-finding shape, and A/B asymmetry;
- high-value at and around the per-currency threshold and the default, with
  `abs()` applied to a negative total;
- lifecycle: `COMPLETED` with findings vs `FAILED` on a forced technical fault;
  explicit retry of a `FAILED` attempt producing `attempt_number` 2; a
  `COMPLETED` attempt is not re-runnable;
- one active attempt per normalization; concurrent starts → exactly one wins;
  illegal starts (non-`COMPLETED` normalization, already validated) → `409`;
- transactional persistence: a forced write failure leaves zero finding rows
  and a `FAILED` attempt;
- retrieval and `404` / `409` behaviour across all five endpoints;
- every `documents` / `invoice_extractions` / `invoice_normalizations*` row
  unchanged after a run (byte-for-byte), and the stored PDF untouched;
- database relationships, constraints, and migration upgrade / downgrade with
  Stage 2–4 data preserved;
- the boundary tests in §1.7 (no decision vocabulary in the contract);
- explicit proof that validation makes no AI or external-network call
  (a `socket`-blocked engine run over a rich invoice, and a source scan of the
  validation package).

---

## Open questions for the reviewer

The ⚠ items, collected:

1. **Required fields** — is `{invoice_number, invoice_date, vendor_name,
   currency, total_amount}` the right required set, or should `subtotal` /
   `tax_amount` / at least one line item be required too?
2. **Monetary tolerance** — flat `0.01` absolute, or add a relative component
   for large invoices? Should JPY-class currencies use exact equality (or a
   vendored per-currency minor-unit tolerance) instead of the flat `0.01`?
3. **Line-sum tolerance** — `max(0.01, 0.01 × line_count)`, or a flat value?
4. **Line-sum target precedence** — `subtotal` → `total_amount − tax_amount` →
   `total_amount`; confirm the fallbacks are wanted rather than "skip".
5. **Date windows** — `due_date > invoice_date + 365d` and `invoice_date <
   run date − 10y`; keep, retune, or drop these soft date rules? Is the
   `run date` dependence for future/old dates acceptable?
6. **Confidence threshold** — `0.70` for critical fields; and confirm
   `null` → `info` (`critical_field_confidence_unavailable`) rather than
   "not evaluated".
7. **High-value thresholds** — the per-currency magnitudes and the `10 000`
   default are placeholders; provide real numbers. Compare `abs(total_amount)`
   or the signed value?
8. **Duplicate key** — vendor identity (`vendor_tax_id` else `vendor_name`) +
   `invoice_number` + `currency` + `total_amount` within tolerance; exact
   match only. Add fuzzy vendor-name matching now or defer? Is exposing a
   matched `document_id` in a finding acceptable pre-multi-tenancy?
9. **Re-validation** — a `COMPLETED` validation is frozen and the current MVP
   exposes no re-validation route. Acceptable for the MVP?
