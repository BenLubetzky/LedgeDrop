# Stage 8: reviewer corrections and re-decision — specification

This is the detailed specification for Stage 8. `CLAUDE.md` carries the
high-level summary and the scope limits; this file holds the correction
boundary, the pinned policy, the persistence design, the merge projection, the
lifecycle/status matrix, the API, the reviewer UI, the implementation order,
and the verification map.

**Status. Complete.**

* **Package 1 — done.** This document; the internal contract
  (`app/schemas/correction.py`, `tests/test_correction_contract.py`); the
  persistence models (`app/models/correction.py`) and migration
  `0008_correction_tables` (verified `upgrade → downgrade → upgrade` on a
  throwaway PostgreSQL database with `alembic check` clean at head — the
  Stage 7 migration test now targets `0005_decision_tables` explicitly so it
  survives migrations stacked above it); the persistence bridge
  (`app/schemas/correction_persistence.py`,
  `tests/test_correction_persistence.py`).
* **Package 2 — done.** The merge projection
  (`app/services/processing/correction/projection.py`,
  `tests/test_correction_projection.py`); the lifecycle guards + status matrix
  (`lifecycle.py`, `tests/test_correction_lifecycle.py`); the orchestrating
  service (`service.py`); the API (`app/api/corrections.py`,
  `app/schemas/correction_api.py`) and `get_correction_service` wiring; the
  pre-Stage-8 fake-`human-correction`-extraction shortcut is removed.
  `tests/test_corrections_api.py` drives the composed
  upload → pipeline → correct flow. Full backend suite green.
* **Package 3 — done.** The correction editor posts the new contract, renders
  before/after entries and correction history, supports line-item add/remove,
  and is linked from every correctable dashboard status.
* **Package 4 — done.** The focused contract/lifecycle/persistence/projection/
  API and Stage 8 verification suites cover the composed workflow, concurrent
  submissions, and recovery after a failure that occurs after projection.
  Migration round-trip coverage and the backend/frontend regressions are
  green. The local migration is applied; `alembic check` reports only two
  pre-existing Stage 3 index/model drifts, outside Stage 8.

**Policy calls are business judgement calls**, not derived facts. The four that
were open at the start of Package 1 were pinned by the project owner and are
recorded in Part 2 with their rationale:

1. corrections are a **separate append-only store** with a `normalized ⊕
   corrections` merge projection — never a rewrite of any Stage 2–7 row (the
   pre-Stage-8 shortcut, which wrote a fake `human-correction` extraction
   attempt, is removed);
2. **any** post-processing document status is correctable — `NEEDS_REVIEW`,
   `COMPLETED`, `APPROVED`, `REJECTED`;
3. a correction that re-decides `ACCEPTED` **auto-accepts** (to `APPROVED` from
   `NEEDS_REVIEW` / `APPROVED` / `REJECTED`; staying `COMPLETED` from
   `COMPLETED`); a correction that re-decides `NEEDS_REVIEW` returns the
   document to the review queue;
4. every correction spawns a **new attempt chain** (normalization → validation
   → decision), all attempts kept, so the full edit history and every
   re-decision is queryable and the terminal outcome is always "the latest
   decision attempt plus any Stage 7 review recorded on it".

Changing any pinned value later is a doc edit plus the matching
code/migration edit, not a design change.

---

## Part 1 — Correction boundary

### 1.1 What Stage 8 is

Stage 8 records a **human correction** of the canonical invoice data for a
document that has finished processing, then re-runs the deterministic stages
(normalization projection → validation → decision) against the corrected data
and resolves the document's status from the new outcome.

Its input is:

- one document whose `status` is `NEEDS_REVIEW`, `COMPLETED`, `APPROVED`, or
  `REJECTED` (Part 2.2);
- that document's **current processing chain** — its latest extraction
  attempt, that extraction's latest normalization attempt (the *base*), that
  normalization's latest validation attempt, and that validation's latest
  **`COMPLETED`** decision attempt (the staleness anchor, Part 2.6);
- a **submitted corrected invoice** — the full set of canonical scalar fields
  plus the full desired line-item list, as free text, exactly the shape a
  reviewer edits on screen;
- reviewer attribution — a required, explicitly *unverified* `reviewer_name`
  and an optional `note` (Part 2.3).

Its output is:

- one **correction attempt** (`invoice_corrections` row) with an ordered list
  of **field-level correction entries** (`invoice_correction_fields`) — a
  before/after diff of exactly the fields the reviewer changed, plus
  line-item add/remove markers;
- a new **normalization attempt** holding the `base ⊕ corrections` merged
  canonical values (produced by projection, **not** by re-running the Stage 4
  engine on the extraction), a new **validation attempt** over it, and a new
  **decision attempt** over that;
- at most **one** authorized `documents.status` transition, per the Part 2.4
  matrix.

### 1.2 What Stage 8 must NOT do

- **Mutate any Stage 2–7 row or the stored PDF.** Every `invoice_extractions`,
  `invoice_line_items`, existing `invoice_normalizations`,
  `invoice_normalized_line_items`, `invoice_normalization_errors`,
  `invoice_validations`, `invoice_validation_findings`, `invoice_decisions`,
  `invoice_decision_reasons`, and `invoice_reviews` row stays byte-for-byte
  unchanged, and so do the original PDF bytes. A correction adds *new* attempt
  rows beside the immutable ones; it never edits or deletes an existing
  attempt. The only field Stage 8 writes on an existing row is
  `documents.status` (and its `updated_at`).
- **Re-run extraction or the Stage 4 normalization engine.** The corrected
  canonical values come from the merge projection (Part 3), which applies the
  Stage 4 *field normalizers* to the reviewer's input but never re-reads the
  provider output. Extraction attempts are never added by Stage 8.
- **Call an AI provider or make an external-network call.** Stage 8 is pure
  database, deterministic Python, and HTTP. The re-run validation and decision
  stages are themselves offline and deterministic.
- **Override a Stage 6 `ACCEPTED` result without a correction.** There is no
  "just approve it" path in Stage 8 — a status change only ever follows a
  recorded correction and its re-decision. (Stage 7 still owns the plain
  approve/reject of a `NEEDS_REVIEW` decision with no data change.)
- **Invent invoice data.** A field the reviewer leaves blank is a `null`
  canonical value (and, if the field is required, a Stage 5
  `missing_required_field` finding on the re-run), never a guess or a carried
  forward stale value beyond the explicit merge rule (Part 3.3).
- **Authenticate or authorize.** LedgerDrop has no auth layer; any caller who
  can reach the API may submit a correction, and `reviewer_name` is a display
  label only (Part 2.3), exactly as in Stage 7.

### 1.3 Lifecycle position

`documents.status` gains **no new value**. Stage 8 re-uses the existing states
and adds transitions *into* `APPROVED` / `NEEDS_REVIEW` / `COMPLETED` from a
correction:

```
NEEDS_REVIEW ─(correction re-decides ACCEPTED)────────────────▶ APPROVED
NEEDS_REVIEW ─(correction re-decides NEEDS_REVIEW)────────────▶ NEEDS_REVIEW  (re-queued)
COMPLETED    ─(correction re-decides ACCEPTED)────────────────▶ COMPLETED     (unchanged)
COMPLETED    ─(correction re-decides NEEDS_REVIEW)────────────▶ NEEDS_REVIEW
APPROVED     ─(correction re-decides ACCEPTED)────────────────▶ APPROVED      (unchanged value, new chain)
APPROVED     ─(correction re-decides NEEDS_REVIEW)────────────▶ NEEDS_REVIEW  (re-opened for review)
REJECTED     ─(correction re-decides ACCEPTED)────────────────▶ APPROVED
REJECTED     ─(correction re-decides NEEDS_REVIEW)────────────▶ NEEDS_REVIEW
```

`APPROVED` reached this way carries the same meaning as a Stage 7 `APPROVE`:
the invoice is cleared. The difference is provenance — a Stage 7 approval has
an `invoice_reviews` row; a Stage 8 auto-accept has a `COMPLETED`
`invoice_corrections` row whose `resulting_decision` is `ACCEPTED`. Both are
terminal in the sense that the document is not in the queue, but neither is
frozen: a further correction can always be submitted (Part 2.2).

### 1.4 The correction-attempt lifecycle

A correction attempt has its **own** technical lifecycle, mirroring Stages
4–6:

```
correctable document ─(submit)─▶ PROCESSING ─▶ COMPLETED   (resulting_decision stored)
                                          └──▶ FAILED       (technical only, no outcome)
FAILED correction ─(explicit retry)─▶ PROCESSING ─▶ COMPLETED | FAILED
```

- `PROCESSING` is durable before any downstream work runs — the
  `invoice_corrections` row and its field entries are committed first, so an
  interrupted run leaves a visible record.
- `COMPLETED` means the merge projection, the new normalization attempt, the
  new validation attempt, and the new decision attempt all ran and the
  decision attempt reached a business `outcome` (`ACCEPTED` or `NEEDS_REVIEW`).
  A `NEEDS_REVIEW` re-decision is a **successful** `COMPLETED` correction, not
  a failure.
- `FAILED` is a technical fault only: the merge projection raised, a
  persistence write failed, or the re-run validation/decision attempt ended
  technically `FAILED`. A `FAILED` correction leaves `documents.status`
  untouched, records a client-safe `failure_code` / `failure_message`, keeps
  whatever downstream attempts it did create (each independently retryable via
  its own stage endpoint), and is itself retryable — `retry` re-reads the
  stored submission and runs a fresh correction attempt against the document's
  now-current chain.
- At most one `PROCESSING` correction attempt exists per document (a partial
  unique index enforces it); a concurrent second submit loses the race and
  gets `409 CORRECTION_IN_PROGRESS`.

---

## Part 2 — Pinned correction policy

`CORRECTION_POLICY_VERSION` (`app/schemas/correction.py`) names this revision;
it is stamped onto every `invoice_corrections` row at creation and never
changed, so a historical correction stays explicable if the policy is retuned
later (mirroring `decision_catalogue.POLICY_VERSION` and
`REVIEW_POLICY_VERSION`).

### 2.1 The correction store is separate and append-only

A correction is recorded in a **new** `invoice_corrections` /
`invoice_correction_fields` pair (Part 4). These tables:

- **reference** the source normalization attempt, the source decision attempt,
  and the owning document by foreign key — the "source chain" — and record
  which new normalization / validation / decision attempts the correction
  produced;
- **never write** to any Stage 2–7 row. The corrected canonical values are
  persisted as a *new* `invoice_normalizations` attempt (Part 3.5) tagged with
  `source_correction_id`, sitting beside the Stage 4 engine's own attempt,
  never replacing it;
- are **append-only**: field entries are written once at submit and never
  edited; rows are removed only by the `ON DELETE CASCADE` from deleting the
  owning document. `retry` adds a new correction attempt (higher
  `attempt_number`); it does not rewrite the failed one.

The pre-Stage-8 `POST /documents/{id}/corrections` endpoint
(`app/api/corrections.py`, `app/schemas/correction_api.py`) wrote a synthetic
`invoice_extractions` attempt with `provider_name = "human-correction"` and
re-ran the chain from there. That violated the Stage 3 invariant that an
extraction row is provider output and the Stage 8 "never write a Stage 2–7
row" rule. It is **removed** by this stage and replaced by
`POST /documents/{id}/corrections` with the contract in Part 6.

### 2.2 Which documents are correctable

A correction may be submitted when `documents.status` is one of
`NEEDS_REVIEW`, `COMPLETED`, `APPROVED`, `REJECTED`
(`CORRECTABLE_DOCUMENT_STATUSES`). `UPLOADED`, `PROCESSING`, and `FAILED` are
rejected with `409 DOCUMENT_NOT_CORRECTABLE` — there is no finished chain to
correct.

A correctable document must additionally have a **`COMPLETED` decision
attempt** as the latest attempt on its current chain; without one there is no
staleness anchor and nothing to re-decide, so `409 DECISION_NOT_COMPLETED`.
This is always true for a `NEEDS_REVIEW` / `APPROVED` / `REJECTED` document and
for a `COMPLETED` document that ran the pipeline through the decision stage; a
`COMPLETED` document whose validation or decision never completed is not
correctable until it does.

`APPROVED` and `REJECTED` are deliberately **not frozen**: the recent product
direction is that a reviewer who spots a wrong value on an already-resolved
invoice fixes it in place rather than re-uploading. The audit trail stays
intact because the prior decision attempt, and any Stage 7 review on it, are
untouched — the correction simply adds a newer chain that becomes current.

### 2.3 Reviewer attribution (pre-authentication)

Every correction carries a **required, free-text `reviewer_name`** (trimmed,
non-empty, ≤ 200 characters) and an **optional `note`** (if present: trimmed,
non-empty, ≤ 4000 characters — a blank note is rejected, not stored as `""`).
`reviewer_name` is stored and returned as an **explicitly unverified label** —
never an authenticated principal, never used for authorization, only for
display in the audit trail. When authentication arrives it is replaced by a
verified identity reference. This matches Stage 7's `reviewer_name` framing
exactly.

There is **no permission model**: any caller who can reach the API may submit
a correction for any correctable document.

### 2.4 Auto-accept versus return-to-review (the status matrix)

After the re-run decision attempt reaches a `COMPLETED` outcome, the document
status is resolved by `resolve_document_status(origin_status, outcome)`:

| Decision re-run `outcome` | `origin_status` (at submit) | New `documents.status` |
|---|---|---|
| `ACCEPTED` | `COMPLETED` | `COMPLETED` (unchanged) |
| `ACCEPTED` | `NEEDS_REVIEW` | `APPROVED` |
| `ACCEPTED` | `APPROVED` | `APPROVED` (unchanged value) |
| `ACCEPTED` | `REJECTED` | `APPROVED` |
| `NEEDS_REVIEW` | *any correctable* | `NEEDS_REVIEW` |

Rationale:

- **Auto-accept, not a second human step.** The correction *is* the human
  step: a person looked at the PDF, fixed the data, and the deterministic
  rules then cleared it. Forcing another `NEEDS_REVIEW` round-trip on a clean
  re-decision would be busywork. From `COMPLETED` the value already means
  "extraction finished" and Stage 6 leaves an `ACCEPTED` document there, so
  the correction matches that (the accepted-ness is only knowable from the
  decision attempt, exactly as in Stage 6 §6.2).
- **Return-to-review on any lingering problem.** If the re-run still gates
  (`missing_required_field`, `totals_do_not_reconcile`, a
  `normalization_error` from a value the reviewer typed that Stage 4 cannot
  parse, …), the document goes to `NEEDS_REVIEW` against the **new** decision
  attempt and re-appears in the Stage 7 queue with the new reasons. A
  previously `APPROVED` or `REJECTED` document that re-decides `NEEDS_REVIEW`
  is genuinely re-opened — its terminal status is no longer accurate once the
  data changed.
- The re-run `DecisionService` already writes `documents.status = NEEDS_REVIEW`
  on that outcome (Stage 6 §6.2); the correction service performs only the
  *additional* writes the matrix above requires (the `ACCEPTED` → `APPROVED`
  cases) and never has to undo a Stage 6 write.

### 2.5 The terminal outcome is always the latest decision attempt

Because every correction spawns a new normalization → validation → decision
chain and all attempts are kept, "what did LedgerDrop conclude about this
invoice" is answered by walking the document's current chain to its latest
`COMPLETED` decision attempt and reading its `outcome`, plus any
`invoice_reviews` row on that decision. A correction never mutates or
supersedes the *record* of an earlier decision; it only makes a newer one
current. The `invoice_corrections` rows, oldest first, are the ordered edit
log; each points at the decision attempt it produced.

### 2.6 Stale source

The submit body carries `source_decision_id` (required) and
`source_normalization_id` (optional). If, when the correction is processed,
that decision is no longer the document's current chain's latest `COMPLETED`
decision — or the supplied `source_normalization_id` is not the current base —
the submit is refused with `409 STALE_CORRECTION_SOURCE` and nothing is
written. This mirrors Stage 6's `STALE_VALIDATION_SOURCE` and Stage 7's
`STALE_DECISION_SOURCE`. Unlike those, this guard **is** reachable through the
API in normal use: two reviewers open the same invoice, one submits a
correction, the second's stale submit is rejected and they reload to see the
new chain.

### 2.7 Correction content: what a field entry records, and line items

`invoice_correction_fields` is a server-computed **diff** of the submitted
invoice against the base normalization attempt — one row per field the
reviewer actually changed, never a full copy. Each row records:

- `operation` — `SET_FIELD` (a scalar or an existing line-item leaf changed),
  `ADD_LINE_ITEM` (the submitted list is longer — one row per appended index),
  or `REMOVE_LINE_ITEM` (the submitted list is shorter — one row per dropped
  index);
- `field_path` — a Stage 4 scalar name, `line_items.<index>.<leaf>`, or
  `line_items.<index>` for an add/remove marker;
- `previous_value` — the base canonical value, stringified, or `null`;
- `raw_value` — the reviewer's submitted text, or `null` when they cleared the
  field or for a `REMOVE_LINE_ITEM`;
- `normalized_value` — the canonical result of applying the Stage 4 field
  normalizer to `raw_value`, or `null` (clean absence **or** a normalization
  error);
- `error_code` / `error_message` — set together, from the closed
  `NormalizationErrorCode` set, when `raw_value` could not be normalized. An
  entry with an error still records; it is *data about the correction*, not a
  technical failure, and it flows into the merged projection as a `null` value
  plus a `NormalizationError`, so the re-run validation raises the appropriate
  finding.

Line-item add/remove is index-aligned and applied in one pass (Part 3.4): the
submitted list length sets the merged list length; leaves are normalized
per-field; a shorter list drops the tail. Re-ordering is not modelled — a
reviewer who reorders lines is recorded as a set of `SET_FIELD` edits on the
overlapping indices plus add/remove on the difference.

### 2.8 Repeated corrections

A document may be corrected any number of times. Each `submit` resolves the
*current* chain fresh, so the second correction's base is the first
correction's output normalization attempt. `attempt_number` on
`invoice_corrections` is per-document and increments across the whole edit
history (including `retry`). There is no cap.

---

## Part 3 — The merge projection (`app/services/processing/correction/projection.py`)

Pure, deterministic, no session, no AI, no network. The single place the
`base ⊕ corrections` canonical result is computed.

### 3.1 Inputs

- `base: NormalizedInvoice` — rebuilt from the source normalization attempt
  via `normalized_invoice_from_attempt` (so it is already contract-valid).
- `submitted: SubmittedInvoice` — a small model in `app/schemas/correction_api.py`
  holding the ten canonical scalars as `str | None` and `line_items` as a list
  of `{description, quantity, unit_price, line_total}` string dicts. Blank
  strings are normalized to `None` on the way in (`extra="forbid"`).

### 3.2 Output

`merge_correction(base, submitted) -> tuple[NormalizedInvoice, list[CorrectionFieldEntry]]`
— the merged canonical invoice (re-validated through `NormalizedInvoice`, so
the `YYYY-MM-DD` date shape, ISO-4217 currency shape, decimal typing, and the
"a field with an error has a null value" rule all hold), and the ordered diff
entries for persistence.

### 3.3 Scalar merge rule

For each of the ten scalars, in contract order:

1. Run the **same Stage 4 normalizer** the engine uses for that field
   (`_SCALAR_NORMALIZERS` in `normalization/engine.py`, re-exported so there is
   one source of truth) over `submitted.<field>`.
2. The merged value is the normalizer's canonical value, or `None` (clean
   absence or error). On error, add a `NormalizationError` for that
   `field_path` to the merged `errors`.
3. If the merged value (or its error state) differs from `base.<field>` /
   `base`'s error for that field, emit a `SET_FIELD` `CorrectionFieldEntry`
   with `previous_value = str(base value)` and `raw_value = submitted text`.
4. If it does **not** differ, no entry is emitted and the base value (and any
   base error for that field) is carried through unchanged.

A field the reviewer did not touch — same text as the base's canonical form —
produces no entry and no change.

### 3.4 Line-item merge rule

Let `b = len(base.line_items)`, `s = len(submitted.line_items)`.

- Indices `0 .. min(b, s) - 1`: diff each of the four leaves exactly as a
  scalar (per-field normalizer, `SET_FIELD` entry on change,
  `field_path = line_items.<i>.<leaf>`).
- Indices `min(b, s) .. s - 1` (when `s > b`): one `ADD_LINE_ITEM` entry per
  index (`field_path = line_items.<i>`, `raw_value = null`), plus a
  `SET_FIELD` entry for each non-null leaf of the new line.
- Indices `min(b, s) .. b - 1` (when `b > s`): one `REMOVE_LINE_ITEM` entry per
  dropped index (`field_path = line_items.<i>`, `previous_value` = a compact
  `"desc | qty | unit | total"` summary of the dropped line, `raw_value =
  null`).

The merged `line_items` list is built from the submitted list (length `s`),
each leaf normalized; dropped tail lines simply do not appear.

### 3.5 Persisting the projection as a normalization attempt

`CorrectionRepository.persist_projection(...)` writes the merged
`NormalizedInvoice` as a **new** `invoice_normalizations` row:

- `extraction_id` = the base attempt's `extraction_id` (same source
  extraction);
- `attempt_number` = `NormalizationRepository.next_attempt_number(extraction_id)`
  (so it is the extraction's newest normalization attempt and any
  `latest_for_extraction` / stale-source check treats it as current);
- `status = COMPLETED`, `started_at` / `completed_at` = now;
- `source_correction_id` = the correction attempt (the provenance marker;
  `NULL` on every Stage 4-engine attempt);
- scalar columns, `invoice_normalized_line_items`, and
  `invoice_normalization_errors` filled from the merged contract via the
  existing `app/schemas/normalization_persistence.py` flatteners.

The Stage 4 engine is **not** invoked. This attempt is immutable once written,
exactly like an engine attempt.

---

## Part 4 — Persistence (`app/models/correction.py`, migration `0008_correction_tables`)

### 4.1 `invoice_corrections` (ORM `CorrectionAttempt`)

One row per correction attempt.

| column | type | notes |
|---|---|---|
| `correction_id` | `Uuid` PK | `default uuid4` |
| `document_id` | `Uuid` FK → `documents.document_id` `ON DELETE CASCADE` | the corrected document |
| `attempt_number` | `Integer` | 1-based, per document, increments across `submit` + `retry`; `UNIQUE(document_id, attempt_number)` |
| `source_normalization_id` | `Uuid` FK → `invoice_normalizations.normalization_id` `ON DELETE CASCADE` | the base attempt merged against |
| `source_decision_id` | `Uuid` FK → `invoice_decisions.decision_id` `ON DELETE CASCADE` | the `COMPLETED` decision the reviewer acted on (staleness anchor) |
| `status` | native enum `correction_status` (`PROCESSING` / `COMPLETED` / `FAILED`) | |
| `reviewer_name` | `String(200)` `NOT NULL` | unverified label (Part 2.3) |
| `note` | `Text` `NULL` | optional, non-blank if present |
| `policy_version` | `String(32)` `NOT NULL` | `CORRECTION_POLICY_VERSION` at creation |
| `origin_document_status` | native enum `document_status` `NOT NULL` | the status at submit — input to the Part 2.4 matrix and the audit record |
| `submitted_payload` | `JSONB` `NOT NULL` | the raw submitted invoice, internal-only (never in a public response), so `retry` is exact and the diff is auditable |
| `resulting_normalization_id` | `Uuid` FK → `invoice_normalizations.normalization_id` | set once the projection is persisted |
| `resulting_validation_id` | `Uuid` FK → `invoice_validations.validation_id` | set once validation ran |
| `resulting_decision_id` | `Uuid` FK → `invoice_decisions.decision_id` | set on `COMPLETED` |
| `resulting_outcome` | native enum `decision_outcome` `NULL` | mirror of the re-run decision's outcome; set on `COMPLETED` |
| `resulting_document_status` | native enum `document_status` `NULL` | where the document ended (Part 2.4); set on `COMPLETED` |
| `failure_code` / `failure_message` | `String(64)` / `Text` `NULL` | client-safe; set only when `FAILED` |
| `submitted_at` | `DateTime(tz)` `NOT NULL` | audit timestamp |
| `completed_at` | `DateTime(tz)` `NULL` | |
| `created_at` / `updated_at` | `DateTime(tz)` `NOT NULL` | bookkeeping |

**Indexes / constraints:**

- `UNIQUE(document_id, attempt_number)` — `uq_invoice_corrections_document_id_attempt_number`.
- partial unique `uq_invoice_corrections_one_active_per_document` on
  `document_id WHERE status = 'PROCESSING'` — one in-flight correction per
  document (the concurrency backstop; the service also `SELECT ... FOR UPDATE`s
  the `documents` row).
- `ck_invoice_corrections_attempt_number_positive`: `attempt_number >= 1`.
- `ck_invoice_corrections_reviewer_name_not_blank`:
  `length(btrim(reviewer_name)) > 0`.
- `ck_invoice_corrections_note_not_blank`:
  `note IS NULL OR length(btrim(note)) > 0`.
- `ck_invoice_corrections_status_fields_consistent`:
  - `PROCESSING` → `completed_at`, `failure_code`, `failure_message`,
    every `resulting_*` column all `NULL`;
  - `COMPLETED` → `completed_at`, `resulting_normalization_id`,
    `resulting_validation_id`, `resulting_decision_id`, `resulting_outcome`,
    `resulting_document_status` all `NOT NULL`; `failure_code` /
    `failure_message` `NULL`;
  - `FAILED` → `completed_at`, `failure_code`, `failure_message` `NOT NULL`;
    `resulting_decision_id`, `resulting_outcome`, `resulting_document_status`
    `NULL` (`resulting_normalization_id` / `resulting_validation_id` may be
    non-null — they name attempts the failed run did create and that stay
    individually retryable).

**Relationships:** `Document` gains
`corrections: Mapped[list["CorrectionAttempt"]]`
(`order_by="CorrectionAttempt.attempt_number"`, `cascade="all, delete-orphan"`,
`passive_deletes=True`). `NormalizationAttempt` gains
`source_correction: Mapped["CorrectionAttempt | None"]` on the
`source_correction_id` FK (see 4.3).

The ORM class is `CorrectionAttempt` (not `InvoiceCorrection`) to stay
distinct from the Pydantic contract type, matching every other stage.

### 4.2 `invoice_correction_fields` (ORM `CorrectionFieldRow`)

The ordered diff for one correction attempt (Part 2.7).

| column | type | notes |
|---|---|---|
| `correction_field_id` | `Uuid` PK | |
| `correction_id` | `Uuid` FK → `invoice_corrections.correction_id` `ON DELETE CASCADE` | |
| `position` | `Integer` | 0-based order; `UNIQUE(correction_id, position)` |
| `operation` | native enum `correction_operation` (`SET_FIELD` / `ADD_LINE_ITEM` / `REMOVE_LINE_ITEM`) | |
| `field_path` | `Text` `NOT NULL` | scalar name, `line_items.<i>.<leaf>`, or `line_items.<i>`; `UNIQUE(correction_id, field_path)` |
| `previous_value` | `Text` `NULL` | base canonical value, stringified |
| `raw_value` | `Text` `NULL` | reviewer text |
| `normalized_value` | `Text` `NULL` | canonical result of normalizing `raw_value` |
| `error_code` | native enum `normalization_error_code` `NULL` | reuses the Stage 4 enum |
| `error_message` | `Text` `NULL` | |
| `created_at` | `DateTime(tz)` `NOT NULL` | |

**Constraints:**

- `ck_invoice_correction_fields_position_non_negative`: `position >= 0`.
- `ck_invoice_correction_fields_error_pair`:
  `(error_code IS NULL) = (error_message IS NULL)`.
- `ck_invoice_correction_fields_field_path_shape`: scalar name in the Stage 4
  list, **or** `field_path ~ '^line_items\.(0|[1-9][0-9]*)(\.(description|quantity|unit_price|line_total))?$'`
  (built from the `NORMALIZED_SCALAR_FIELD_NAMES` /
  `NORMALIZED_LINE_ITEM_FIELD_NAMES` tuples so it cannot drift).

### 4.3 `invoice_normalizations.source_correction_id`

A nullable column + FK → `invoice_corrections.correction_id`
(`ON DELETE SET NULL`), added by `0008`. `NULL` on every Stage 4-engine
attempt; set on every projection attempt (Part 3.5). It is the provenance
marker that distinguishes "the machine normalized this" from "a reviewer
corrected this". The two tables reference each other
(`invoice_corrections.source_normalization_id` /
`resulting_normalization_id` ↔ `invoice_normalizations.source_correction_id`);
the cycle is safe because rows are created in order — the correction row
first (`resulting_* NULL`), then the projection normalization row (its
`source_correction_id` already resolvable), then the correction row's
`resulting_*` update.

### 4.4 Persistence bridge (`app/schemas/correction_persistence.py`)

- `correction_field_rows(entries: list[CorrectionFieldEntry]) -> list[dict]` —
  flatten the contract entries to `invoice_correction_fields` column dicts,
  `position`-numbered in list order.
- `invoice_correction_from_row(row, field_rows) -> InvoiceCorrection` — rebuild
  and re-validate the contract from a stored attempt (re-running the closed
  `operation` / `error_code` enums and the `field_path` shape on the way out),
  ignoring `submitted_payload` and the `resulting_*` columns.

### 4.5 Migration `0008_correction_tables`

`down_revision = "0007_defer_decision_reason_finding_fk"`. `upgrade()` creates
the two native enums (`correction_status`, `correction_operation`), the two
tables, and the `invoice_normalizations.source_correction_id` column + FK.
`downgrade()` drops the column + FK, the two tables, and the two enums. No
`document_status` change, so no rename-swap is needed. Verified by an
`upgrade → downgrade → upgrade` round trip on a throwaway PostgreSQL database
with Stage 2–7 rows preserved byte-for-byte and `alembic check` clean at head.

---

## Part 5 — Repository, lifecycle, service (Package 2)

### 5.1 `CorrectionRepository` (`app/services/processing/correction/repository.py`)

Sole reader/writer of `invoice_corrections` / `invoice_correction_fields`. No
transaction boundary — stages objects, runs queries; the service commits.

- `get(correction_id)`, `get_for_document(document_id, correction_id)`,
  `list_for_document(document_id)` (oldest first),
  `latest_for_document(document_id)`, `active_for_document(document_id)`,
  `next_attempt_number(document_id)`.
- `add_attempt(*, document_id, attempt_number, source_normalization_id,
  source_decision_id, correction: InvoiceCorrection, submitted_payload,
  policy_version, origin_document_status, submitted_at) -> CorrectionAttempt`
  — build the row + `correction_field_rows(...)` children, stage, return.
- `persist_projection(*, extraction_id, correction_id, merged: NormalizedInvoice)
  -> NormalizationAttempt` — Part 3.5.
- `finalize(attempt, *, resulting_normalization_id, resulting_validation_id,
  resulting_decision_id, resulting_outcome, resulting_document_status)` and
  `record_partial(attempt, *, resulting_normalization_id=None,
  resulting_validation_id=None)` / `mark_failed(attempt, code, message)`.

### 5.2 Lifecycle (`app/services/processing/correction/lifecycle.py`)

Pure guards, `ConflictError` (409) for caller-driven, `ValueError` for
internal:

- `ensure_document_can_be_corrected(document_status)` —
  `DOCUMENT_NOT_CORRECTABLE` unless in `CORRECTABLE_DOCUMENT_STATUSES`.
- `ensure_source_decision_completed(decision_status)` —
  `DECISION_NOT_COMPLETED`.
- `ensure_decision_is_current_source(*, source_decision_id,
  current_decision_id, source_normalization_id, current_normalization_id)` —
  `STALE_CORRECTION_SOURCE`.
- `ensure_no_active_correction(active)` — `CORRECTION_IN_PROGRESS`.
- `ensure_can_retry(latest_status)` — `CORRECTION_NOT_FAILED` unless the
  latest attempt is `FAILED`.
- `ensure_attempt_transition(current, new)` — `PROCESSING → COMPLETED |
  FAILED` only.
- `resolve_document_status(*, origin_status, outcome) -> DocumentStatus` — the
  Part 2.4 matrix, as data (`_STATUS_MATRIX`) so the service and the tests
  read one source of truth.

### 5.3 `CorrectionService` (`app/services/processing/correction/service.py`)

`submit(document_id, *, submitted, reviewer_name, note, source_decision_id,
source_normalization_id, manual_review_requested=False) -> CorrectionAttempt`:

1. `SELECT ... FOR UPDATE` the `documents` row.
2. `ensure_document_can_be_corrected(document.status)`.
3. Resolve the current chain: `ExtractionRepository.latest_for_document` →
   `NormalizationRepository.latest_for_extraction` →
   `ValidationRepository.latest_for_normalization` →
   `DecisionRepository.latest_for_validation`.
   `ensure_source_decision_completed`; `ensure_decision_is_current_source`
   against the submitted anchors.
4. `ensure_no_active_correction(active_for_document)`.
5. `merge_correction(base, submitted)` → `(merged, entries)`. A raised
   exception here is a caller-data problem surfaced as `422` (the submitted
   payload is malformed beyond what the schema caught) — it does **not**
   create a `PROCESSING` row.
6. Create the `PROCESSING` `invoice_corrections` row + entries; `flush`
   (`IntegrityError` → `409 CORRECTION_IN_PROGRESS`); `commit`. (Durable
   before downstream work.)
7. `persist_projection` → new normalization attempt; `commit`;
   `record_partial(resulting_normalization_id=...)`.
8. `ValidationService(session).start(new_normalization_id)`;
   `record_partial(resulting_validation_id=...)`. If it ended technically
   `FAILED` → `mark_failed(...)`, `commit`, return.
9. `DecisionService(session).start(new_validation_id,
   manual_review_requested=...)`. If it ended technically `FAILED` →
   `mark_failed(...)`, `commit`, return. (A `NEEDS_REVIEW` outcome is **not**
   failed — `DecisionService` has already set `documents.status = NEEDS_REVIEW`
   in its own transaction.)
10. Re-`SELECT ... FOR UPDATE` the `documents` row;
    `new_status = resolve_document_status(origin_status, outcome)`; set
    `document.status = new_status` (a no-op write when the matrix says
    "unchanged"); `finalize(attempt, resulting_* = ...)`;
    `ensure_attempt_transition(PROCESSING, COMPLETED)`; `commit`.
11. Return the refreshed correction attempt.

Any unexpected exception between step 6 and step 10 rolls back and falls
through to `mark_failed` against the committed `PROCESSING` row, leaving
`documents.status` exactly as the last committed sub-stage left it (Stage 6
may already have written `NEEDS_REVIEW`; a `FAILED` correction does not undo
that — the new chain genuinely does need review).

`retry(correction_id)`: load the `FAILED` attempt, `ensure_can_retry`, re-read
`submitted_payload`, and run `submit`'s steps 1–11 as a **new** correction
attempt (`attempt_number + 1`) against the document's now-current chain.

`CorrectionService` makes **no AI or external-network call**; the re-run
validation and decision services are themselves offline.

---

## Part 6 — API (`app/api/corrections.py`)

All routes hang off the document (a correction is about the whole invoice, not
one upstream attempt), mirroring the review queue's flat shape.

| Method / path | Purpose | Errors |
|---|---|---|
| `POST /documents/{document_id}/corrections` | Submit a correction; `201` + `InvoiceCorrectionResult`, document status resolved | `404 DOCUMENT_NOT_FOUND`; `409 DOCUMENT_NOT_CORRECTABLE` / `DECISION_NOT_COMPLETED` / `STALE_CORRECTION_SOURCE` / `CORRECTION_IN_PROGRESS`; `422` for a bad body |
| `POST /documents/{document_id}/corrections/{correction_id}/retry` | Re-run a technically failed correction as a new attempt; `201` | `404 DOCUMENT_NOT_FOUND` / `CORRECTION_NOT_FOUND`; `409 CORRECTION_NOT_FAILED` / … |
| `GET /documents/{document_id}/corrections` | Every correction attempt for the document, newest first | `404 DOCUMENT_NOT_FOUND` |
| `GET /documents/{document_id}/corrections/latest` | The most recent attempt | `404 DOCUMENT_NOT_FOUND` / `CORRECTION_NOT_FOUND` |
| `GET /documents/{document_id}/corrections/{correction_id}` | One specific attempt | `404 DOCUMENT_NOT_FOUND` / `CORRECTION_NOT_FOUND` |

### 6.1 Request schema (`app/schemas/correction_api.py`)

`InvoiceCorrectionRequest` (`extra="forbid"`):

- `source_decision_id: uuid.UUID` (required), `source_normalization_id:
  uuid.UUID | None`.
- `reviewer_name: str` (trimmed, 1–200), `note: str | None` (trimmed,
  1–4000 if present).
- `manual_review_requested: bool = False`, `strict=True` — forwarded to the
  re-run decision stage exactly as on `POST /documents/{id}/pipeline`
  (add-only; it can only push a re-decision toward `NEEDS_REVIEW`).
- The ten canonical scalars as `str | None` and `line_items: list[
  CorrectionLineItemInput]` (`{description, quantity, unit_price, line_total}`,
  all `str | None`). Blank strings become `None`. This *is* the
  `SubmittedInvoice` the projection consumes.

### 6.2 Response schema

`InvoiceCorrectionResult` (`extra="forbid"`, client-safe only):

- `correction_id`, `document_id`, `attempt_number`, `status`
  (`PROCESSING` / `COMPLETED` / `FAILED`), `reviewer_name`, `note`,
  `policy_version`, `origin_document_status`, `resulting_document_status`,
  `resulting_outcome`, `submitted_at`, `completed_at`,
  `failure_code` / `failure_message`.
- `entries: list[CorrectionFieldEntryView]` — `operation`, `field_path`,
  `previous_value`, `raw_value`, `normalized_value`, `error_code`,
  `error_message`. The before/after diff the UI renders.
- `pipeline: PipelineRunResult | null` — the re-run
  `extraction` (unchanged, for context) + new `normalization` / `validation` /
  `decision` public results, reusing `PipelineRunResult.from_attempts`, present
  on a `COMPLETED` correction (and on a `FAILED` one as far as it got).
- `submitted_payload` is **never** exposed. `raw_response`, storage paths,
  hashes, and internal attempt ids beyond those above are never exposed.

`get_correction_service` is added to `app/api/deps.py` (session-bound, no
provider, mirroring `get_review_service`). `app/api/router.py` keeps
`include_router(corrections.router)`.

---

## Part 7 — Reviewer UI (Package 3)

Next.js App Router, existing plain CSS. The Stage 7 review-detail screen
already renders an editable **Field / value** grid and a line-item editor; the
Stage 8 changes are:

- The "Validate & approve" action `POST`s `InvoiceCorrectionRequest` to
  `/documents/{id}/corrections` (the new contract), not the removed shortcut.
- On a `COMPLETED` `ACCEPTED` correction (`resulting_outcome === "ACCEPTED"`),
  show a success banner ("Corrected and approved") and a link back to the
  queue.
- On a `COMPLETED` `NEEDS_REVIEW` correction, re-render the detail pane from
  `pipeline` (new findings / reasons) and show a "still needs review" notice —
  the reviewer keeps editing against the new chain.
- A **before/after** block driven by `entries` — one row per changed field,
  `previous_value → normalized_value` (or `raw_value` + the error message when
  `error_code` is set), grouped scalars then line-item adds/removes.
- Line-item **add** (append a blank line) and **remove** (drop a line) buttons
  in the editor; the submitted `line_items` array length drives
  `ADD_LINE_ITEM` / `REMOVE_LINE_ITEM` server-side.
- A **correction history** list (`GET /documents/{id}/corrections`) on the
  detail screen: attempt number, reviewer, when, `resulting_outcome`,
  changed-field count.
- A dashboard "Edit" affordance on `COMPLETED` / `APPROVED` / `REJECTED`
  documents opens the same editor (already partly present from the recent
  "edit approved documents" work) pointed at the new endpoint.

`src/lib/*` gains only client-safe types. No `submitted_payload`, no
`policy_version` surfaced in the UI beyond a tooltip.

---

## Part 8 — Implementation order

1. **This document + the internal contract.** `app/schemas/correction.py`
   (`CorrectionStatus`, `CorrectionOperation`, `CorrectionFieldEntry`,
   `InvoiceCorrection`, `CorrectedInvoiceResult`, `CORRECTION_POLICY_VERSION`,
   `CORRECTABLE_DOCUMENT_STATUSES`), with `tests/test_correction_contract.py`.
2. **Persistence + migration + bridge + repository.**
   `app/models/correction.py`, migration `0008_correction_tables`,
   `app/schemas/correction_persistence.py`,
   `app/services/processing/correction/repository.py`, with
   `tests/test_correction_persistence.py` and the composed API tests.
3. **Merge projection.**
   `app/services/processing/correction/projection.py` (re-exporting the Stage 4
   `_SCALAR_NORMALIZERS` / `_LINE_ITEM_NORMALIZERS`), with
   `tests/test_correction_projection.py` — scalar diff, line add/remove,
   error passthrough, no-op edit, idempotence.
4. **Lifecycle + service + API + wiring; remove the shortcut.**
   `app/services/processing/correction/{lifecycle,service}.py`,
   `app/schemas/correction_api.py` rewrite, `app/api/corrections.py` rewrite,
   `get_correction_service` in `deps.py`; delete the fake-extraction code
   path. Tests: `tests/test_correction_lifecycle.py`,
   `tests/test_corrections_api.py` (rewritten).
5. **Reviewer UI (Package 3).** Part 7.
6. **End-to-end verification + docs (Package 4).** `tests/test_stage8_verification.py`,
   migration round-trip test, README + `CLAUDE.md` status updates — only after
   the whole backend suite and `npm run lint` / `npx next build --webpack` are
   green.

---

## Part 9 — Verification map

| Checklist item | Where it is proven |
|---|---|
| `CorrectionStatus` / `CorrectionOperation` closed; `InvoiceCorrection` trims/bounds `reviewer_name`, non-blank `note`; `extra="forbid"` | `tests/test_correction_contract.py` |
| `CORRECTABLE_DOCUMENT_STATUSES` is exactly `{NEEDS_REVIEW, COMPLETED, APPROVED, REJECTED}`; `resolve_document_status` matches the Part 2.4 matrix cell for cell | `tests/test_correction_contract.py`, `tests/test_correction_lifecycle.py` |
| `invoice_corrections` unique on `(document_id, attempt_number)` and partial-unique on one `PROCESSING` per document; field-path and error-pair invariants | migration + contract/persistence tests and composed API suite |
| `invoice_normalizations.source_correction_id` is `NULL` on an engine attempt and set on a projection attempt | `tests/test_corrections_api.py` + migration round trip |
| `correction_field_rows` / `invoice_correction_from_row` round-trip every valid contract and re-validate on rebuild | `tests/test_correction_persistence.py` |
| Repository reads/writes, projection persistence, finalize/failure, and concurrent submission behavior | `tests/test_corrections_api.py`, `tests/test_stage8_verification.py` |
| Merge projection: untouched field → no entry; scalar change → `SET_FIELD` with `previous`/`raw`/`normalized`; unparseable value → entry with `error_code` + merged `NormalizationError`; line append → `ADD_LINE_ITEM` + leaf `SET_FIELD`s; line drop → `REMOVE_LINE_ITEM`; `merge(merge(x)) == merge(x)` for a clean submission | `tests/test_correction_projection.py` |
| Guards: non-correctable status → `DOCUMENT_NOT_CORRECTABLE`; no `COMPLETED` decision → `DECISION_NOT_COMPLETED`; superseded chain → `STALE_CORRECTION_SOURCE`; second submit → `CORRECTION_IN_PROGRESS`; retry a non-failed → `CORRECTION_NOT_FAILED` | `tests/test_correction_lifecycle.py`, `tests/test_corrections_api.py` |
| `submit` from `NEEDS_REVIEW` with a clean fix → new chain, decision `ACCEPTED`, document `APPROVED`, correction `COMPLETED` with `resulting_*` set | `tests/test_corrections_api.py` |
| `submit` from `NEEDS_REVIEW` that still gates → document stays `NEEDS_REVIEW` against the new decision attempt, re-appears in `/reviews/queue` with the new reasons | `tests/test_stage8_verification.py` |
| `submit` from `COMPLETED` clean → stays `COMPLETED`; from `COMPLETED` now-gating → `NEEDS_REVIEW` | `tests/test_stage8_verification.py` |
| `submit` from `APPROVED` / `REJECTED`: clean → `APPROVED`; gating → `NEEDS_REVIEW` (re-opened) | `tests/test_stage8_verification.py` |
| Repeated corrections: second `submit`'s base is the first's projection attempt; `attempt_number` increments; both `invoice_corrections` rows kept and queryable | `tests/test_stage8_verification.py` |
| Earlier processing attempts remain addressable while every correction creates new normalization/validation/decision ids; the stored PDF path is never opened by the correction subsystem | composed correction API tests + correction repository boundary |
| Concurrency: two simultaneous submits produce exactly one completed correction and one `409` | `tests/test_stage8_verification.py::test_concurrent_submissions_keep_one_outcome` |
| Failure recovery: a fault after projection → correction `FAILED`; retry replays the original source as a fresh attempt and succeeds | `tests/test_corrections_api.py::test_retry_after_failure_past_projection_replays_original_source` |
| Migration `0008` upgrade → downgrade → upgrade on real PostgreSQL with Stage 2–7 rows preserved; `alembic check` clean at head | migration verification recorded in Package 1 and `tests/test_review_migration.py` regression coverage |
| No AI call, no external-network call anywhere in the correction subsystem | `tests/test_stage8_verification.py::test_correction_subsystem_has_no_ai_or_network_import` |

**Scope limits.** Corrections, re-projection, re-validation, re-decision, and
the auto-accept/return-to-review resolution only. Notifications, reviewer
assignment, authentication/organizations, and downstream posting remain
later-stage work. Re-ordering line items (as distinct from add/remove + edit)
and correcting a document that never reached a `COMPLETED` decision are out of
scope.
