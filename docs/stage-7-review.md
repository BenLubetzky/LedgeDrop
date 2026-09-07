# Stage 7: human review — specification

This is the detailed specification for Stage 7, the final stage of the invoice
MVP. `CLAUDE.md` carries the high-level summary and the scope limits; this file
holds the review boundary, the pinned review policy, the internal contract, and
the persistence design.

**Status.** Complete. Package 1 (this document, the contract, persistence,
repository, migration), Package 2 (lifecycle guards, the review service, the
API — Part 7), Package 3 (the reviewer interface — Part 8), and Packages 4–5
(resolution/audit integration and end-to-end verification — Part 9 and
"## Verification") are all done. Field corrections and reprocessing are
explicitly **Stage 8** (see "## Corrections and reprocessing — deferred to
Stage 8").

**Policy calls are business judgement calls**, not derived facts. The three
that were open at the start of Package 1 have been pinned by the project owner:
terminal document statuses (`APPROVED` / `REJECTED` added), reviewer attribution
(a required but explicitly *unverified* name), and resolution notes (required on
`REJECT`, optional on `APPROVE`). Changing any pinned value later is a doc edit
plus the matching code/migration edit, not a design change.

---

## Part 1 — Review boundary

### 1.1 What Stage 7 is

Stage 7 records a **human resolution** of an invoice that Stage 6 routed to
review. Its input is one **completed** Stage 6 decision attempt
(`invoice_decisions` row, `status = COMPLETED`, `outcome = NEEDS_REVIEW`) that is
still the owning document's current processing chain. Its output is a single,
terminal **review record** — `APPROVE` or `REJECT`, attributed and timestamped,
with a note when policy requires one — plus the one authorized `documents.status`
transition.

The MVP has three surfaces: a **review queue** of documents awaiting a human
decision, a **read-only review view** that shows the original PDF and every
upstream result with provenance, and the **approve / reject** action itself.

### 1.2 What Stage 7 must NOT do

- **Reinterpret or recompute anything upstream.** It does not re-run Stage 5
  rules or Stage 6 policy, re-score confidence, or recalculate totals. It
  records a verdict on the decision that already exists.
- **Mutate any upstream record.** Every `documents` row field except `status`
  (and its `updated_at`), and every `invoice_extractions`,
  `invoice_line_items`, `invoice_normalizations`,
  `invoice_normalized_line_items`, `invoice_normalization_errors`,
  `invoice_validations`, `invoice_validation_findings`, `invoice_decisions`, and
  `invoice_decision_reasons` row, plus the stored PDF bytes, stay byte-for-byte
  unchanged.
- **Override a Stage 6 `ACCEPTED` outcome.** Only a `NEEDS_REVIEW` decision is
  reviewable. There is no "manually reject a clean invoice" path.
- **Edit invoice data or reprocess.** No corrected canonical values, no
  re-extraction, no re-normalization triggered by a review. Those need a new
  canonical-data and lineage policy and are out of scope for the whole stage.
- **Call an AI provider or make an external-network call.** Stage 7 is pure
  database and HTTP.
- **Notify, assign, or authenticate.** No reviewer notifications, no assignment
  workflow, no auth/roles/orgs.

### 1.3 Lifecycle position

`documents.status` gains two terminal states:

```
UPLOADED -> PROCESSING -> COMPLETED            (extraction finished; Stage 3)
                       -> FAILED
COMPLETED -> NEEDS_REVIEW                       (Stage 6 decision = NEEDS_REVIEW)
NEEDS_REVIEW -> APPROVED                        (Stage 7 review action = APPROVE)
NEEDS_REVIEW -> REJECTED                        (Stage 7 review action = REJECT)
```

`COMPLETED` keeps its existing meaning — *extraction finished* — and Stage 6
still leaves an `ACCEPTED` invoice at `COMPLETED`. `APPROVED` and `REJECTED` are
reachable **only** from `NEEDS_REVIEW`, **only** through Stage 7, and are
terminal: nothing transitions a document out of them in the MVP (reprocessing,
which could, is out of scope).

A review is a single terminal event, not an async attempt: there is no
`PROCESSING` review, no technical `FAILED` review, and no retry chain. A guard
that blocks a resolution (unknown id, decision not reviewable, already resolved,
stale source) returns an HTTP error and writes nothing.

---

## Part 2 — Pinned review policy

`REVIEW_POLICY_VERSION` (`app/schemas/review.py`) names this revision; it is
stamped onto every review record at write time and never changed afterwards, so
a historical resolution stays explicable if the policy is retuned later
(mirroring `decision_catalogue.POLICY_VERSION`).

### 2.1 Reviewer actions and terminal outcomes

Exactly two actions: **`APPROVE`** and **`REJECT`**. Each produces one terminal
review record and the matching terminal document status (`APPROVE -> APPROVED`,
`REJECT -> REJECTED`). There is no third action, no "defer", no "request
changes" in the MVP.

### 2.2 Permissions

There is **no permission model** in the MVP, because LedgerDrop has no
authentication layer. Any caller who can reach the API may resolve any queued
item. A real permission/role check is deferred to the authentication stage; this
is documented, not implied.

### 2.3 Reviewer attribution (pre-authentication)

Every resolution carries a **required, free-text `reviewer_name`** (trimmed,
non-empty, ≤ 200 characters). It is stored and returned as an **explicitly
unverified label** — it is never treated as an authenticated principal, never
used for authorization, and never trusted for anything but display in the audit
trail. When authentication arrives, `reviewer_name` is replaced by a verified
identity reference; the column and its "unverified" framing exist only to give
the MVP a usable audit trail in the meantime.

### 2.4 Resolution notes

- **`REJECT`**: a `note` is **required** — present, trimmed, non-empty,
  ≤ 4000 characters. A rejection must be justified.
- **`APPROVE`**: a `note` is **optional**. If supplied it must be non-blank and
  ≤ 4000 characters; an empty or whitespace-only note is rejected rather than
  stored as `""`.

The rule is enforced in three places that must agree: the Pydantic contract
(`InvoiceReview`), a table `CHECK` constraint, and (Package 2) the request
schema.

### 2.5 Queue eligibility

A document is **in the review queue** exactly when all hold:

1. its latest `invoice_decisions` attempt (for its current
   extraction → normalization → validation chain) has `status = COMPLETED` and
   `outcome = NEEDS_REVIEW`;
2. that decision has **no** `invoice_reviews` row yet;
3. `documents.status = NEEDS_REVIEW`.

Resolving a document removes it from the queue (its decision now has a review
row and its status is terminal). The queue is ordered oldest decision first
(FIFO) — finalised in Package 2.

### 2.6 Stale source decision

If, at resolution time, the target decision is **no longer** the document's
current chain (a newer extraction/normalization/validation/decision exists), the
resolution is refused with `409 STALE_DECISION_SOURCE` and nothing is written —
mirroring Stage 6's `STALE_VALIDATION_SOURCE` guard. This is a defensive guard:
today's API exposes no way to reprocess a document that already has a decision,
so it is not normally reachable, but the check makes the invariant explicit.

### 2.7 One review per decision; immutability

At most **one** review record per decision attempt, enforced by a `UNIQUE`
constraint on `invoice_reviews.decision_id`. A second resolution attempt —
including a concurrent one — loses the race and gets `409 DECISION_ALREADY_REVIEWED`.

A review record is **append-only**: once written it is never updated or deleted
by application code (only a cascade from deleting the owning document removes
it). There is no "re-open" or "amend" route in the MVP. The audit history of a
document is the ordered set of its decision chains, each with at most one review.

---

## Part 3 — Internal contract (`app/schemas/review.py`)

Pure data definition — no AI, no network, no database. Mirrors
`app/schemas/decision.py`. Unknown keys are rejected (`extra="forbid"`) on every
model.

- **`ReviewAction(str, Enum)`** — `APPROVE = "APPROVE"`, `REJECT = "REJECT"`.
  Exactly two members.
- **`InvoiceReview(BaseModel)`** — identity-free internal contract:
  - `action: ReviewAction`
  - `reviewer_name: str` — validator trims and requires length 1–200 after
    trimming; the trimmed value is stored.
  - `note: str | None` — validator: if present, trimmed length 1–4000 (the
    trimmed value is stored); a whitespace-only string is rejected. A
    model-level validator additionally requires `note is not None` when
    `action is ReviewAction.REJECT`.
- **`ReviewedInvoiceResult(BaseModel)`** — `source_decision_id: uuid.UUID` plus
  `review: InvoiceReview`. Mirrors `DecidedInvoiceResult`; carries the link to
  the source decision attempt and nothing else.
- **`REVIEW_POLICY_VERSION: str`** — the named revision of this document's
  Part 2 policy.
- **`DOCUMENT_STATUS_FOR_ACTION: dict[ReviewAction, DocumentStatus]`** — the
  Part 1.3 map (`APPROVE -> APPROVED`, `REJECT -> REJECTED`), so the lifecycle
  code in Package 2 and the tests read one source of truth. (Kept here as data;
  the status write itself is Package 2.)
- Derived tuples: `REVIEW_ACTIONS`, `REVIEW_FIELD_NAMES`.

---

## Part 4 — Persistence (`app/models/review.py`, migration `0006_review_tables`)

### 4.1 `DocumentStatus` change (`app/models/document.py`)

`APPROVED = "APPROVED"` and `REJECTED = "REJECTED"` are appended to the
`DocumentStatus` enum and therefore to the native `document_status` PostgreSQL
type. Because `ALTER TYPE ... ADD VALUE` cannot run in the same transaction that
then uses the value, migration `0006` recreates the type by the rename-swap
pattern instead (rename old → create new with all seven labels → drop the
column default → `ALTER COLUMN ... TYPE ... USING status::text::document_status`
→ restore the default → drop the old type). This runs inside Alembic's
transaction and round-trips: `downgrade()` swaps the type back to the original
five labels, which succeeds as long as no row is `APPROVED`/`REJECTED` — and by
then `invoice_reviews` has been dropped, so nothing produces those values. A
`USING` cast that hits a real `APPROVED`/`REJECTED` row fails loudly, which is
the correct behaviour for a downgrade that would lose audit state.

### 4.2 `invoice_reviews` table (ORM `ReviewRecord`)

One row per terminal resolution. No child table — a review has no structured
reason list of its own (the evidence lives on the Stage 5/6 rows it points at).

| column | type | notes |
|---|---|---|
| `review_id` | `Uuid` PK | `default uuid4` |
| `decision_id` | `Uuid` FK → `invoice_decisions.decision_id` `ON DELETE CASCADE` | **`UNIQUE`** — one review per decision (Part 2.7) |
| `action` | native enum `review_action` (`APPROVE`, `REJECT`) | |
| `reviewer_name` | `String(200)` `NOT NULL` | unverified label (Part 2.3) |
| `note` | `Text` `NULL` | required iff `action = REJECT` (Part 2.4) |
| `policy_version` | `String(32)` `NOT NULL` | `REVIEW_POLICY_VERSION` at write time |
| `reviewed_at` | `DateTime(timezone=True)` `NOT NULL` | the moment the resolution was recorded — the audit timestamp |
| `created_at` | `DateTime(timezone=True)` `NOT NULL` | row bookkeeping |

There is deliberately no `updated_at`: the row is immutable by design (Part 2.7),
and omitting the column makes an accidental in-place update stand out in review.

**CHECK constraints** (names via `op.f(...)`, matching the house pattern):

- `ck_invoice_reviews_reviewer_name_not_blank`: `length(btrim(reviewer_name)) > 0`
- `ck_invoice_reviews_note_shape`:
  `(action = 'REJECT' AND note IS NOT NULL AND length(btrim(note)) > 0) OR (action = 'APPROVE' AND (note IS NULL OR length(btrim(note)) > 0))`

**Relationship**: `DecisionAttempt` gains
`review: Mapped["ReviewRecord | None"]` (`uselist=False`,
`cascade="all, delete-orphan"`, `passive_deletes=True`,
`back_populates="source_decision"`). Deleting a document still cascades the
whole branch (document → extraction → normalization → validation → decision →
review) through the existing `ON DELETE CASCADE` chain.

The ORM class is `ReviewRecord` (not `InvoiceReview`) to keep it distinct from
the Pydantic contract type, exactly as Stage 6 uses `DecisionAttempt` vs
`InvoiceDecision`.

### 4.3 Persistence bridge (`app/schemas/review_persistence.py`)

`review_row(review: InvoiceReview) -> dict` flattens the contract to the
`action` / `reviewer_name` / `note` columns; `invoice_review_from_row(row) ->
InvoiceReview` rebuilds and re-validates it (running the closed action enum and
the note rule again on the way out). One small module, mirroring
`decision_persistence.py`, so the flat layout is derived in exactly one place.

---

## Part 5 — Repository (`app/services/processing/review/repository.py`)

`ReviewRepository` is the only reader/writer of `invoice_reviews`. It owns no
transaction boundary — it stages objects and runs queries; the Package 2 service
decides when to flush and commit. Mirrors `DecisionRepository`.

- `get(review_id) -> ReviewRecord | None`
- `get_for_decision(decision_id, review_id) -> ReviewRecord | None` — by id, but
  only if it belongs to `decision_id`
- `get_by_decision(decision_id) -> ReviewRecord | None` — the one review for a
  decision, if it exists
- `exists_for_decision(decision_id) -> bool`
- `add(*, decision_id, review: InvoiceReview, policy_version, reviewed_at) ->
  ReviewRecord` — build the row via `review_row(...)`, stage it, return it; the
  caller flushes (a `UNIQUE` violation surfaces there as the "already reviewed"
  race)

The cross-cutting **queue query** (documents whose current decision is an
unreviewed `NEEDS_REVIEW`) is Package 2 — it spans `documents` and the whole
chain, not just `invoice_reviews`.

---

## Part 6 — Package 1 verification map

| Item | Where |
|---|---|
| `ReviewAction` closed to two members; `InvoiceReview` trims/bounds `reviewer_name`; blank name rejected | `tests/test_review_contract.py` |
| `note` required and non-blank for `REJECT`; optional but non-blank for `APPROVE`; over-length rejected | `tests/test_review_contract.py` |
| `ReviewedInvoiceResult` carries only `source_decision_id` + `review`; unknown keys rejected | `tests/test_review_contract.py` |
| `DOCUMENT_STATUS_FOR_ACTION` maps `APPROVE→APPROVED`, `REJECT→REJECTED` and nothing else | `tests/test_review_contract.py` |
| `review_row` / `invoice_review_from_row` round-trip every valid contract; rebuild re-validates | `tests/test_review_persistence.py` |
| `invoice_reviews` unique on `decision_id`; both CHECKs reject the bad shapes at the DB | `tests/test_review_model.py` |
| `document_status` type carries all seven labels; a `Document` can be set `APPROVED`/`REJECTED` | `tests/test_review_model.py` |
| Deleting a `Document` cascades to its `ReviewRecord` | `tests/test_review_model.py` |
| Repository reads (`get`, `get_for_decision`, `get_by_decision`, `exists_for_decision`) and `add`; the second `add` for one decision raises on flush | `tests/test_review_repository.py` |
| Migration `0006_review_tables` upgrade → downgrade → upgrade on real PostgreSQL with Stage 2–6 rows preserved byte-for-byte; `document_status` loses/regains `APPROVED`/`REJECTED`; `alembic check` clean at head | `tests/test_review_migration.py` |

---

## Part 7 — Lifecycle, service, and API (package 2)

### 7.1 A review has no attempt lifecycle

Unlike Stages 3–6, a review is a **single terminal event**, not an attempt with
a status. There is no `PROCESSING` review, no technical `FAILED` review row, and
no `attempt_number`/retry chain:

- **"Claiming/locking"** is a pessimistic row lock at submit time
  (`SELECT ... FOR UPDATE OF invoice_decisions, documents`), not a separate
  claim record or claim endpoint. Without an identity layer a per-reviewer
  claim/lock would be unenforceable and misleading, so the MVP omits it: the
  queue simply shows unresolved items and the first submit wins.
- **"Safe retry"** means a technical submit failure persists **nothing** — the
  whole transaction rolls back, `documents.status` stays `NEEDS_REVIEW`, and the
  caller submits again. There is no retry route because there is no partial
  state to resume. A *successful* review is terminal: it cannot be amended or
  re-opened (that would need the deferred correction/lineage policy).

`app/services/processing/review/lifecycle.py` holds the pure guards:
`ensure_decision_can_be_reviewed` (`COMPLETED` + `NEEDS_REVIEW`, not already
reviewed), `ensure_document_awaiting_review` (owning document still
`NEEDS_REVIEW` — catches an inconsistent state), and
`ensure_decision_is_current_source` (the whole extraction → normalization →
validation → decision chain is still the document's latest). Each raises
`ConflictError` (409); there is no internal-transition guard because there is
no internal transition.

### 7.2 `ReviewService.submit`

`app/services/processing/review/service.py`. In one transaction:
`_lock_source` resolves and locks the target `invoice_decisions` row and its
owning `documents` row; the three lifecycle guards run; the currency check
compares the decision's chain against the document's current
extraction/normalization/validation; then the `invoice_reviews` row is written
and `documents.status` is moved `NEEDS_REVIEW → APPROVED | REJECTED` per
`DOCUMENT_STATUS_FOR_ACTION`; `flush` + `commit`. A concurrent second submit
that races past the lock hits the `UNIQUE (decision_id)` constraint and is
turned into a `409 DECISION_ALREADY_REVIEWED`. Any other exception rolls the
transaction back and propagates (generic 500) with nothing persisted. The
service makes **no AI or external-network call**.

### 7.3 API (`app/api/reviews.py`)

| Method / path | Purpose | Errors |
|---|---|---|
| `GET /reviews/queue` | Pending items, oldest decision first (FIFO); `limit` (1–200, default 50) + `offset` | — |
| `GET /reviews/{review_id}` | One recorded resolution, with its `document_id` | `404 REVIEW_NOT_FOUND` |
| `POST .../validations/{vid}/decisions/{did}/review` | Submit `APPROVE` / `REJECT`; `201` + `InvoiceReviewResult`, document moved | `404` per broken chain link (`DOCUMENT_NOT_FOUND` … `DECISION_NOT_FOUND`); `409 DECISION_NOT_REVIEWABLE` / `DECISION_ALREADY_REVIEWED` / `STALE_DECISION_SOURCE`; `422` for a bad/empty body |
| `GET .../decisions/{did}/review` | The resolution for a decision, if any | `404 DECISION_NOT_FOUND` / `REVIEW_NOT_FOUND` |

The submit body (`ReviewSubmitRequest`) *is* the internal contract
`InvoiceReview` — same closed `action`, trimmed/bounded `reviewer_name`, and
the "note required for `REJECT`" rule, `extra="forbid"`. An empty body is a
`422` (an action and a reviewer are mandatory). `InvoiceReviewResult` exposes
`review_id`, `decision_id`, `document_id`, `action`, `reviewer_name` (the
unverified label), `note`, `policy_version`, `reviewed_at`, `created_at` — and
nothing internal. `ReviewQueueEntry` additionally carries the chain ids and the
ordered Stage 6 `decision` reasons (rebuilt and re-validated) so the queue can
show *why* each invoice was flagged. Deep nesting on the scoped routes matches
every other stage and gives each broken link its own 404.

### 7.4 Package 2 verification map

| Item | Where |
|---|---|
| Guards: reviewable only when `COMPLETED`+`NEEDS_REVIEW`+unreviewed; `ACCEPTED`/`PROCESSING`/`FAILED` rejected; stale chain (any hop) rejected | `tests/test_review_lifecycle.py` |
| `submit` records the review, stamps `policy_version`, and moves the document `NEEDS_REVIEW → APPROVED`/`REJECTED` | `tests/test_review_service.py`, `tests/test_reviews_api.py` |
| Unknown decision → `DECISION_NOT_FOUND`; non-reviewable → `409` and nothing written | `tests/test_review_service.py`, `tests/test_reviews_api.py` |
| Second/concurrent submit → exactly one wins, other `409 DECISION_ALREADY_REVIEWED`; first outcome kept | `tests/test_review_service.py::test_two_concurrent_submits_only_one_wins`, `tests/test_reviews_api.py::test_concurrent_http_submits_only_one_wins` |
| Superseded chain → `STALE_DECISION_SOURCE` | `tests/test_review_service.py::test_stale_decision_source_is_rejected` |
| Every Stage 2–6 row unchanged after a submit; only `documents.status` moves | `tests/test_review_service.py::test_source_rows_are_untouched_apart_from_document_status` |
| Queue lists pending items with reasons, FIFO, and drops resolved ones; `GET /reviews/{id}` + decision-scoped `GET` | `tests/test_reviews_api.py` |
| Bad/empty/unknown-key body → `422`; each broken chain link → its own `404` | `tests/test_reviews_api.py` |

---

## Part 8 — Reviewer interface (package 3)

Next.js App Router screens under `frontend/`, styled with the existing plain
CSS in `src/app/globals.css` (no UI framework), talking to the same FastAPI
backend as the upload dashboard.

- **`/review`** (`src/components/review-queue.tsx`) — `GET /reviews/queue`.
  One card per pending invoice: filename, when it was flagged, the ordered
  Stage 6 reason messages as chips (gating reasons tinted), and a link to the
  review screen. Loading / error / empty states mirror the dashboard.
- **`/review/[documentId]`** (`src/components/review-detail.tsx`) — a
  two-pane screen. Left: the original PDF in an `<iframe>` on
  `GET /documents/{id}/file` (`Content-Disposition: inline`), with an
  "open in a new tab" fallback. Right, scrollable: the decision reasons (why
  it needs review), the validation findings (severity, humanised rule, field
  label, expected/actual), a **Field / Extracted / Normalized** table with
  per-field normalization errors surfaced inline, the normalized line items,
  and the **Approve / Reject** form. The screen resolves the
  extraction → normalization → validation → decision chain itself via the
  `latest` endpoints, so it is refreshable and deep-linkable; a document whose
  status is no longer `NEEDS_REVIEW` shows a "not awaiting review" notice
  instead of the form.
- **Approve / Reject.** A reviewer name is always required (sent as the
  unverified `reviewer_name`); a note is required to reject and optional to
  approve — the buttons disable until their rule is met, matching the
  contract. On success the form is replaced by a resolution banner and a link
  back to the queue; `409` codes map to plain sentences
  (`DECISION_ALREADY_REVIEWED`, `DECISION_NOT_REVIEWABLE`,
  `STALE_DECISION_SOURCE`).
- **No internal diagnostics.** `src/lib/review-types.ts` models only the
  client-safe fields; finding `context`, raw provider payloads, `policy_version`,
  file hashes, storage paths, and attempt ids beyond what a URL needs are never
  requested or rendered. `failure_code` / `failure_message` are not shown (a
  `NEEDS_REVIEW` document has no failed stage).
- The dashboard's top bar gains a "Review queue" link; no other dashboard
  behaviour changes.

**Frontend build.** `npm run lint` is clean and `npx next build --webpack`
type-checks and compiles all routes (`/`, `/review`, `/review/[documentId]`,
`/_not-found`). Turbopack cannot run on this machine — Smart App Control blocks
Next's unsigned native SWC binary, the same constraint noted for the Python
toolchain — so the webpack builder is used; the WASM fallback still
type-checks. There is no frontend unit-test harness in this repo yet.

---

## Part 9 — Resolution integration and audit behaviour (package 4)

Package 4's deliverables landed inside Package 2 rather than as separate code —
they are properties of `ReviewService.submit` and the queue query, not a new
layer:

- **Terminal status applied atomically with the record.** The
  `invoice_reviews` INSERT and the `documents.status`
  `NEEDS_REVIEW → APPROVED | REJECTED` UPDATE happen in one transaction
  (Part 7.2); a failure rolls back both.
- **Resolved items leave the queue.** `ReviewRepository.queue` filters on
  `documents.status = NEEDS_REVIEW`, `decision.outcome = NEEDS_REVIEW`,
  `decision.status = COMPLETED`, `NOT EXISTS (review for this decision)`, and
  every chain hop at its greatest `attempt_number` — so a resolved or
  superseded decision cannot appear.
- **Historical reads preserved.** `GET /reviews/{review_id}` and
  `GET .../decisions/{did}/review` keep returning the record after the document
  is terminal; the row is append-only (no `updated_at`, no update path).
- **Safe audit data exposed.** `InvoiceReviewResult` carries who
  (`reviewer_name`, the unverified label), what (`action`, `note`), and when
  (`reviewed_at`, `created_at`), plus `document_id` and `decision_id` — and no
  internal diagnostics.
- **Not in scope, confirmed:** no field editing, corrected values,
  reprocessing, notifications, or downstream posting (the first three are
  Stage 8).

---

## Verification

`backend/tests/test_stage7_verification.py` is the executable pass over this
checklist. Like the Stage 4-6 verification suites it drives scenarios through
the *composed stack* — upload → extraction → normalization → validation →
decision → review, over real HTTP — and points bullets with dense coverage at
their per-package file.

| Checklist item | Where it is proven |
|---|---|
| Only a `COMPLETED` `NEEDS_REVIEW` decision is reviewable; a Stage 6 `ACCEPTED` result cannot be overridden | `test_stage7_verification.py::test_an_accepted_decision_cannot_be_reviewed`; `test_review_lifecycle.py`; `test_reviews_api.py` |
| Each broken chain link (`document`/`extraction`/`normalization`/`validation`/`decision`) → its own `404` | `test_stage7_verification.py::test_submit_404s_per_broken_chain_link`; `test_reviews_api.py` |
| A `NEEDS_REVIEW` invoice is queued with its ordered decision reasons; no internal diagnostics in the entry | `test_stage7_verification.py::test_needs_review_invoice_is_queued_with_its_reasons` |
| An `ACCEPTED` invoice is never queued | `test_stage7_verification.py::test_accepted_invoice_is_never_queued` |
| Queue is FIFO by decision time and drops resolved items | `test_stage7_verification.py::test_queue_is_fifo_and_drops_resolved_items`; `test_reviews_api.py`; `test_review_repository.py::test_queue_excludes_a_decision_from_a_superseded_chain` |
| `APPROVE` → `documents.status = APPROVED`; response carries only client-safe fields | `test_stage7_verification.py::test_approve_moves_the_document_end_to_end` |
| `REJECT` + note → `REJECTED`; a note is required to reject | `test_stage7_verification.py::test_reject_with_note_moves_the_document_end_to_end` / `test_reject_without_a_note_is_422_and_changes_nothing` |
| Second / concurrent submit → `409 DECISION_ALREADY_REVIEWED`, first outcome kept, one review row | `test_stage7_verification.py::test_second_submit_is_409_and_keeps_the_first_outcome` / `test_concurrent_submits_only_one_wins`; `test_review_service.py::test_two_concurrent_submits_only_one_wins` |
| Superseded chain → `409 STALE_DECISION_SOURCE`, nothing written | `test_stage7_verification.py::test_a_superseded_chain_is_rejected_as_stale`; `test_review_service.py::test_stale_decision_source_is_rejected` |
| Recorded review retrievable by id and decision-scoped; carries who/what/when; stable across re-reads and the document's status change | `test_stage7_verification.py::test_recorded_review_is_retrievable_and_stable` |
| `NEEDS_REVIEW → APPROVED \| REJECTED` only; every Stage 2–6 row and the stored PDF unchanged; the `documents` row changes in exactly `status` + `updated_at` | `test_stage7_verification.py::test_reviewing_changes_only_the_document_status`; `test_review_service.py::test_source_rows_are_untouched_apart_from_document_status` |
| Migration `0006_review_tables` upgrade → downgrade → upgrade on real PostgreSQL; Stage 2–6 rows byte-for-byte; `document_status` loses/regains `APPROVED`/`REJECTED`; `alembic check` clean | `test_review_migration.py` |
| DB relationships & constraints (`UNIQUE(decision_id)`, note/reviewer CHECKs, cascade) | `test_review_model.py` |
| No AI call, no external-network call | `test_stage7_verification.py::test_review_subsystem_source_has_no_ai_or_network_import` / `test_review_schema_layer_imports_no_ai_sdk` |

**Result.** The whole backend suite passes, including
`test_stage7_verification.py` (16 tests) and the packages 1–2 files
(`test_review_{contract,persistence,model,repository,migration,lifecycle,
service}.py`, `test_reviews_api.py`). `npm run lint` is clean and
`npx next build --webpack` compiles the frontend. Migration `0006_review_tables`
round-trips on a throwaway PostgreSQL database with every Stage 2–6 row
preserved byte-for-byte and `alembic check` clean at head. A review writes
exactly one `invoice_reviews` row and one `documents.status` transition
(`NEEDS_REVIEW → APPROVED | REJECTED`, plus that row's `updated_at`) — nothing
else on any Stage 2–6 row or the stored PDF.

## Corrections and reprocessing — deferred to Stage 8

**Decision (confirmed with the project owner).** In Stage 7 a reviewer can only
**approve or reject**. Editing individual fields, storing corrected values, and
any revalidation / re-decision that a correction would trigger are **out of
scope for Stage 7** and become **Stage 8**. This was reviewed explicitly, not
inherited by default: the correction surface is a stage's worth of design
(a new canonical-data model plus lineage), it is irreversible-ish once a
persistence shape ships, and Stage 7 packages 4–5 do not depend on it.

Sketch of what Stage 8 will need (not a commitment to a design):

1. **Boundary and policy.** Which fields are correctable; who may correct;
   whether a correction forces another human review or can auto-resolve; what
   happens to an already-recorded Stage 6 decision and to `documents.status`;
   how a correction interacts with the stale-source guard.
2. **Corrections persistence.** A new append-only `invoice_corrections` table
   keyed to the source **normalization attempt**, attributed like a review
   (unverified name until auth exists), with a migration. It must **never**
   write to `invoice_extractions` / `invoice_normalizations` or any other
   Stage 2–6 row — corrections live beside the immutable results, not on top
   of them.
3. **Merge view.** A read-only `normalized ⊕ corrections` projection that
   Stage 5 validation and the Stage 6 decision engine can re-run against
   without mutating anything, plus rules for validating a correction itself
   (same field policies as normalization: dates `YYYY-MM-DD`, decimals, ISO
   currency, etc.).
4. **Revalidation + re-decision orchestration.** A committed lifecycle from
   *correction saved* → *revalidate the merged view* → *re-decide* →
   *back to the queue or auto-accepted*, preserving history and the "one
   authorised `documents.status` write" discipline.
5. **UI.** Inline field editing on the review-detail screen, a before/after
   diff, and re-submit.
6. **Verification.** Byte-for-byte immutability of every Stage 2–7 record and
   the stored PDF across a correction cycle; migration round trip.

Until Stage 8 exists, a reviewer who spots a wrong value **rejects** with a
note explaining the problem; the invoice is then re-uploaded or the note is
actioned outside LedgerDrop.

## Scope limits

Queue, inspect, approve/reject, and audit trail only. Editable corrections and
reprocessing are Stage 8 (see the section above). Notifications,
authentication/organizations, and downstream posting remain later-stage work.
Any frontend work beyond the read-only queue and detail screen should be
separately requested.
