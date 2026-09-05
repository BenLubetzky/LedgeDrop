# Stage 6: decision and escalation — specification

This is the detailed specification for Stage 6. `CLAUDE.md` carries the
high-level summary, the six-package handoff, and the scope limits; this file
holds the decision boundary (package 1), the decision policy (package 1) —
including the complete mapping of all 15 Stage 5 rules to decision reasons —
and the internal decision contract these imply.

**Status.** Complete — all 6 packages are done. Package 1: the boundary and policy
below, plus the internal decision contract in
`backend/app/schemas/decision.py` (with `backend/tests/test_decision_contract.py`,
which carries the §1.7 boundary tests) and the formal reason catalogue in
`backend/app/schemas/decision_catalogue.py` (with
`backend/tests/test_decision_catalogue.py`). Package 2 (Part 4 below):
persistence models, migration, persistence schemas, and a repository. Package
3 (Part 5 below): the pure deterministic engine,
`backend/app/services/processing/decision/engine.py` (with
`backend/tests/test_decision_engine.py`). Package 4 (Part 6 below): the
document-status mapping, lifecycle guards including the stale-source check,
and the orchestrating service — `backend/app/services/processing/decision/
{lifecycle,service}.py` (with `backend/tests/test_decision_{lifecycle,
service}.py`). Package 5 (Part 7 below): the scoped decision routes
(`backend/app/api/decisions.py`), their request/response schemas
(`backend/app/schemas/decision_api.py`), the `DecisionService`
`manual_review_requested` thread-through, and the pipeline's fourth stage
(`backend/app/services/processing/pipeline.py`,
`backend/app/schemas/pipeline_api.py`) — with
`backend/tests/test_decisions_api.py` and additions to
`backend/tests/test_pipeline{,_api}.py`. Package 6 ("## Verification" below):
one executable pass over the acceptance checklist through the composed stack
(`backend/tests/test_stage6_verification.py`), the `0005_decision_tables`
migration round trip, the source-immutability and authorized-status-field
proofs, and the decision-subsystem "no AI / no network" guards.

**Policy calls marked ⚠ are business judgement calls**, not derived facts.
They are documented with their rationale so the reviewer can confirm or
overturn them before Stage 6 goes further; changing one is a doc + catalogue
edit, not a design change.

Stage 6 converts a completed Stage 5 validation attempt into a **business
decision**: accept the invoice, or send it for human review. It makes no AI
call and no external-network call, and it never recomputes anything Stage 5
already decided — it only classifies Stage 5's own findings.

---

## Part 1 — Decision boundary

### 1.1 What Stage 6 is

Given one `COMPLETED` validation attempt (and, optionally, a caller's
manual-review request), Stage 6 evaluates a fixed, closed policy over the
attempt's findings and records one **decision**: an `outcome`
(`ACCEPTED | NEEDS_REVIEW`) and the ordered list of **reasons** that explain
it. Every reason Stage 6 can ever cite maps to exactly one Stage 5
`ValidationRule` or to a manual-review request — there is no free-form or
inferred reason.

### 1.2 Inputs (lineage)

Stage 6 reads, and never writes:

| Input | Source | Notes |
|---|---|---|
| The validation attempt's findings | `invoice_validations` / `invoice_validation_findings` rows by `validation_id` | Must be `status = COMPLETED`. Read as `ValidationFinding` objects — `rule`, `severity`, `field_path` — never re-evaluated. |
| A manual-review request | supplied by the caller when a decision attempt starts (package 5 API) | An explicit ask, independent of any finding. Optional, defaults to absent. |
| The decision reason policy | a vendored `decision_catalogue` module (this package) | Not environment config, for the same reason Stage 5's policy is not. |

Stage 6 does **not** read the normalized invoice, the raw extraction, extraction
confidence, or the original PDF directly. Every fact Stage 6 can act on
already arrived as a Stage 5 finding (Stage 5 itself reads confidence,
reconciliation, and duplicate data and turns it into findings — see
`docs/stage-5-validation.md` §1.2). This is deliberate: Stage 6 has exactly
one job, classifying findings, so it cannot silently diverge from what Stage 5
already asserted.

### 1.3 Outputs

One **decision attempt**, at the contract level (`InvoiceDecision`):

- `outcome` — `ACCEPTED | NEEDS_REVIEW` only. No `REJECTED`, no `APPROVED`, no
  `ESCALATED`. Fully determined by `reasons` (§2.1) — a contract invariant,
  not just an engine convention.
- `reasons` — an ordered list of `DecisionReason`, one per Stage 5 finding
  that maps to a reason (all of them do) plus, when requested, one manual
  reason. A reason carries: `code` (closed catalogue, §2.2), `triggers_review`
  (whether *this* reason alone requires review), `source_rule` (the
  `ValidationRule` it came from, or `null` for the manual reason),
  `field_path` (copied from the finding, or `null`), and a fixed client-safe
  `message`.

A later package adds the attempt-level envelope around this: `status`
(`PROCESSING | COMPLETED | FAILED`, §1.5), timestamps, `failure_code` /
`failure_message`, and the link back to the source validation attempt.

**No reason is discarded.** A finding that does not gate the outcome (for
example `no_line_items` or `critical_field_confidence_unavailable` — see §2.2
for the complete gating/non-gating split) still produces a reason in the
list. An `ACCEPTED` decision can and often will carry non-gating reasons; it
is never a bare `{outcome: ACCEPTED, reasons: []}` unless the validation
attempt itself had no findings at all.

### 1.4 What Stage 6 must NOT do

- No automatic rejection. The only outcomes are `ACCEPTED` and
  `NEEDS_REVIEW`; a human decides rejection later.
- No recording of a human's approval or rejection of a `NEEDS_REVIEW`
  invoice — that is later, separate work (a human-review stage).
- No re-running or re-weighting of Stage 5 rules, no new reconciliation or
  confidence math, no reading of extraction/normalization data directly.
- No mutation of any Stage 2–5 record (documents, extractions, normalizations,
  validations, and their child rows or the stored file).
- No inferring `ACCEPTED` from a bare count of findings or a severity tally.
  Every gating decision is a named, enumerated policy fact about a specific
  rule (§2.2), not a threshold on `ValidationSummary` counts.
- No AI call, no network call. Deciding is a pure function of
  (findings, manual_review_requested) and the policy table.

### 1.5 Lifecycle position (attempt status vs. business outcome)

Stage 6 keeps the technical attempt lifecycle and the business outcome
strictly separate, exactly as Stage 5 keeps `ValidationStatus` separate from
`FindingSeverity`:

```
COMPLETED validation ──(start)──> PROCESSING ──> COMPLETED   (outcome: ACCEPTED | NEEDS_REVIEW)
                                          └────> FAILED        (technical only, no outcome)
FAILED decision ──(explicit retry)──> PROCESSING ──> COMPLETED | FAILED
```

- A decision can start only from a `COMPLETED` validation attempt. A
  `PROCESSING` or `FAILED` validation → the attempt is not started (package 4
  fixes the exact `409` codes, mirroring Stage 5's `NORMALIZATION_NOT_COMPLETED`
  shape). No decision row is created for an incomplete source — Stage 6 never
  fabricates a decision from data that was never validated.
- `NEEDS_REVIEW` is a **successful** `COMPLETED` decision, not a technical
  failure. `FAILED` means only: the source validation could not be read, a
  database write failed, or the engine raised an unexpected exception. A
  `FAILED` attempt persists no partial reasons and leaves every source record
  intact. Package 4 fixes the exact lifecycle guards and retry rule,
  mirroring Stage 4/5.
- Document-status integration (whether/when a document moves to
  `NEEDS_REVIEW`) is pinned in the spec as part of package 4, not here —
  package 1 only fixes the decision attempt's own vocabulary.

### 1.6 Why exactly two outcomes

`ACCEPTED` and `NEEDS_REVIEW` are the only two members of `DecisionOutcome`
because Stage 6's job, per the Stage 6 handoff in `CLAUDE.md`, is to decide
whether an invoice **can** be accepted or **needs** human review — not to
close the loop on that human review. Adding `REJECTED` here would mean Stage 6
auto-rejects invoices without a human ever looking at them, which is
explicitly out of scope; adding `APPROVED` / `DENIED` would mean Stage 6
records a human decision it never received. Both stay out of this contract on
purpose, the same way Stage 5's `ValidationStatus` deliberately excludes
`NEEDS_REVIEW`.

### 1.7 Boundary tests (part of package 1)

Contract-level tests that fail if the boundary is crossed
(`tests/test_decision_contract.py`):

- `DecisionOutcome` contains exactly `ACCEPTED`, `NEEDS_REVIEW` — no
  `REJECTED` / `APPROVED` / `DENIED` / `ESCALATED`;
- `DecisionStatus` contains exactly `PROCESSING`, `COMPLETED`, `FAILED`,
  mirroring `ValidationStatus`;
- `InvoiceDecision.outcome` cannot disagree with `InvoiceDecision.reasons` —
  constructing one that does raises a validation error;
- a reason with `code = manual_review_requested` cannot carry a
  `source_rule`; every other reason's `source_rule` must match its `code`;
- `DecisionReason.field_path` accepts only `null` or a well-formed Stage 4
  path (reusing the exact Stage 5 shape check, `require_stage4_field_path`);
- a non-gating reason survives `InvoiceDecision.from_reasons` unchanged even
  when the overall outcome is `ACCEPTED` (nothing is silently dropped).

---

## Part 2 — Decision policy

### 2.1 The gating rule

Exactly one rule decides the outcome, and it is a presence check, never a
count or a threshold:

```
outcome = NEEDS_REVIEW  if any reason has triggers_review = true
        = ACCEPTED      otherwise
```

There is no ranking or precedence among reasons: every reason that applies is
included in `reasons`, in Stage 5 catalogue order followed by the manual
reason (if requested) last. A clean invoice with, say, three independent
problems is `NEEDS_REVIEW` with three reasons listed — Stage 6 does not pick
"the worst one" and discard the others, and it does not need a tie-break rule
because `triggers_review` is a per-reason boolean fact fixed by policy
(§2.2), not a magnitude to compare.

### 2.2 The complete rule → reason mapping

Every one of Stage 5's 15 rules maps to exactly one `DecisionReasonCode` of
the same name, reusing the rule's own client-safe message verbatim (Stage 6
never invents new text for a rule-derived reason). `triggers_review` is the
actual policy decision for each:

| Stage 5 rule | Stage 5 severity | `triggers_review` | Rationale |
|---|---|---|---|
| `missing_required_field` | error | **true** | The business cannot process the invoice at all without this field. |
| `normalization_error` | error/warning | **true** | A field Stage 4 could not make sense of, required or not, needs a human's eyes on the source document. |
| `due_date_before_invoice_date` | warning | **true** | A real inconsistency, even though occasionally legitimate (credit note) — a human confirms which. |
| `due_date_far_after_invoice_date` | warning | **true** | Same: plausible but worth a human glance before acceptance. |
| `invoice_date_in_future` | warning | **true** | Usually a misread or mis-keyed date. |
| `invoice_date_implausibly_old` | warning | **true** | Same. |
| `totals_do_not_reconcile` | warning | **true** | A financial discrepancy on the invoice's own numbers — the core thing Stage 6 exists to catch. |
| `line_item_amount_mismatch` | warning | **true** | Same, at line-item granularity. |
| `line_items_do_not_sum` | warning | **true** | Same, at the aggregate level. |
| `line_item_sum_not_checked` | info | false ⚠ | The aggregate check was *inconclusive* (a line item is missing its total), not failed — per-line mismatches are still caught individually by `line_item_amount_mismatch`, which does gate. Recorded for context, not itself acted on. |
| `low_confidence_critical_field` | warning | **true** | A known, present confidence score below threshold is a real signal the extraction may be wrong. |
| `critical_field_confidence_unavailable` | info | false ⚠ | See §2.3 — the current real provider (GPT-5-mini) never supplies confidence, so gating on its absence would send every OpenAI-sourced invoice to review regardless of quality and defeat Stage 6's purpose. |
| `probable_duplicate_invoice` | warning | **true** | Real financial risk (double payment); always worth a human check. |
| `high_value_invoice` | info | **true** ⚠ | Stage 5 marks this `info` because it is not a *data* defect — but Stage 6 applies a business-risk lens on top of Stage 5's data-quality lens: a large payment warrants a human's sign-off even when every field is clean. This is a deliberate elevation above Stage 5's severity, not an oversight. |
| `no_line_items` | info | false | Common and benign (e.g. a subscription or services invoice billed as a single amount); Stage 5 itself records it as a fact, not a defect. |

Plus one Stage-6-only reason with no Stage 5 counterpart:

| Reason | `triggers_review` | Rationale |
|---|---|---|
| `manual_review_requested` | **true**, always | A caller explicitly asked for review. It only ever *adds* a reason; it cannot suppress or override a reason a rule already produced, and there is no corresponding "manual accept" that removes a gating reason — that would be an approval decision, out of scope here. |

**Reading the ⚠ rows.** Three of Stage 5's four `info`-severity rules stay
non-gating, matching their severity; one (`high_value_invoice`) is
deliberately elevated. This is the most consequential policy call in this
document along with the confidence call in §2.3 — both are flagged in "Open
questions" below for the reviewer to confirm before packages 2–6 build on
top of them.

This table is exhaustive: `backend/app/schemas/decision_catalogue.py` encodes
it as data (`REASON_POLICIES`) and is checked at import time against both the
`ValidationRule` and `DecisionReasonCode` enums, so the code and this table
cannot silently drift apart.

### 2.3 The missing-confidence decision

Stage 5 already fixed (its own §2.6) that a `null` per-field confidence is
never treated as high or low, and always produces
`critical_field_confidence_unavailable` at `info` severity, never
`low_confidence_critical_field`. Stage 6 must decide, separately, whether
that `info` finding *by itself* requires human review.

**Decision: no.** `critical_field_confidence_unavailable` does not gate
(`triggers_review = false`).

**Why.** The current real extraction provider, OpenAI GPT-5-mini, supplies no
calibrated confidence at all — every field's confidence is `null`,
unconditionally (`docs/provider-selection.md`; `docs/stage-3-extraction.md`).
If unavailable confidence gated review, *every* invoice processed by the real
provider would need review regardless of how clean the rest of the invoice
is, which would make automatic acceptance unreachable in production and
defeat the reason Stage 6 exists. Instead, Stage 6 relies on the
deterministic checks that remain fully effective regardless of confidence
availability — required-field presence, totals and line-item reconciliation,
date plausibility, and duplicate detection — as the primary safety net for a
provider that cannot self-report certainty. This is explicitly a
provider-driven, provisional call: if a future provider supplies calibrated
confidence and the business wants unavailable confidence itself to gate
(distinct from *known low* confidence, which already gates via
`low_confidence_critical_field`), that is a one-line change to
`REASON_POLICY_BY_RULE[ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE]`
and its test's expectation — nothing else in Stage 6 depends on this call.

`critical_field_confidence_unavailable` is never interpreted as *high*
confidence either — it simply carries no weight in the outcome, in either
direction, exactly as Stage 5 fixed for the underlying finding itself.

### 2.4 Manual-review requests

A caller may ask for a decision attempt to require review regardless of what
the deterministic policy would otherwise conclude (for example, an operator
who wants a second set of eyes on a specific invoice for a reason outside the
closed rule catalogue). This is modelled as a boolean input to the decision
engine (package 3) and, eventually, the start/retry request body (package 5),
not as a new kind of finding:

- When present, it produces exactly one `manual_review_requested` reason
  (§2.2), appended after every rule-derived reason.
- It only ever **adds** a reason. It cannot remove or downgrade a
  rule-derived reason, and there is no mechanism here to force `ACCEPTED`
  over a rule's objection — that would be a human override of a data-quality
  finding, which is a different, later capability if ever built.
- A decision with only a manual-review reason and no rule-derived ones is
  still fully explainable: `reasons == [manual_review_requested]`,
  `outcome == NEEDS_REVIEW`.

### 2.5 Failed and unusable upstream processing

Stage 6 never fabricates a decision from data that does not exist:

- If the source validation attempt is not `COMPLETED` (still `PROCESSING`,
  or technically `FAILED`), no decision attempt is created at all. This is a
  precondition failure at the API boundary (package 5's `409`, mirroring
  Stage 5's `NORMALIZATION_NOT_COMPLETED`), not a `FAILED` decision — there is
  nothing yet to decide *about*. In the composed pipeline (package 5), the
  `decision` field of the response is `null` in this case, exactly as
  `validation` is `null` when normalization did not complete.
- If the engine itself faults while deciding a legitimately `COMPLETED`
  validation attempt (an unreadable source row, a database write failure, an
  unexpected exception), the decision attempt is `FAILED` with a generic,
  client-safe failure reason, no partial reasons persisted, and is retryable
  — mirroring Stage 4/5 exactly. This is the only case where Stage 6 produces
  a row with no outcome.
- These two cases are deliberately different: "nothing to decide yet" leaves
  no row and is not an error the caller needs to retry-and-hope on; "we tried
  and hit a technical fault" leaves a `FAILED` attempt the caller can retry
  the same way a failed normalization or validation is retried.

### 2.6 Determinism and no recomputation

A decision is a pure function of the source validation attempt's findings and
the caller's manual-review flag, plus the fixed policy table in §2.2 — no
other input, no clock, no external call. Given the same findings and the same
manual-review flag, a decision is fully reproducible. Stage 6 does not read
`run_date`, extraction confidence, or any other Stage 3–5 input directly
(§1.2) — if Stage 5's own determinism inputs (its own `run_date`, its
as-of duplicate snapshot) ever change, that changes the *findings* Stage 6
receives, not anything Stage 6 computes itself.

### 2.7 Cross-cutting policy

- **`triggers_review` is stored per reason**, not re-derived from the current
  policy table when a decision is read back later. If the business retunes
  §2.2 (for example, un-elevating `high_value_invoice`), only *new* decisions
  reflect the change; a historical decision's `reasons` still show what
  applied when it was made. This mirrors Stage 5 storing `severity` per
  finding rather than deriving it from `rule` at read time.
- **No new numeric policy.** Stage 6 introduces no tolerance, threshold, or
  date window of its own — every number a Stage 6 reason might reference
  (a delta, a confidence, a threshold) is already on the Stage 5 finding it
  came from, not duplicated onto the reason. A `DecisionReason` intentionally
  does not repeat `expected` / `actual` / `context` — the full detail stays on
  the linked validation attempt, identified by the contract's
  `source_validation_id` and persisted by package 2, so nothing can drift out
  of step with Stage 5's own record.
- **Field paths** in reasons reuse the Stage 4/5 vocabulary exactly, via the
  same shape check Stage 5's `ValidationFinding.field_path` uses
  (`require_stage4_field_path`, now shared from `app/schemas/validation.py`).
- **Messages** are fixed and client-safe. Every rule-derived reason reuses
  its Stage 5 message verbatim; the one Stage-6-only message
  (`manual_review_requested`) is a new fixed sentence, held in
  `decision_catalogue.MANUAL_REVIEW_MESSAGE`.

---

## Part 3 — Decision contract

Implemented in `backend/app/schemas/decision.py`:

- `DecisionStatus` — `PROCESSING | COMPLETED | FAILED` (§1.5).
- `DecisionOutcome` — `ACCEPTED | NEEDS_REVIEW` (§1.6).
- `DecisionReasonCode` — the closed, 16-member catalogue (§2.2).
- `DecisionReason` — `code`, `triggers_review`, `source_rule`, `field_path`,
  `message` (§1.3); enforces `source_rule` agrees with `code`.
- `InvoiceDecision` — `outcome`, `reasons`; `from_reasons(reasons)` derives
  `outcome` from `reasons` the same way
  `ValidationSummary.from_findings` derives Stage 5's summary, and a
  model validator makes disagreement between the two a contract error, not
  just an engine bug.
- `DecidedInvoiceResult` — binds an `InvoiceDecision` to its
  `source_validation_id`, mirroring `ValidatedInvoiceResult`.

And the policy in `backend/app/schemas/decision_catalogue.py`:

- `ReasonPolicy` — `code`, `source_rule`, `triggers_review`, `message`.
- `REASON_POLICIES` — the 16-entry table in §2.2, checked at import time
  against both `ValidationRule` and `DecisionReasonCode`.
- `policy_for_rule(rule)` / `manual_review_policy()` — lookups the package 3
  engine will use to turn a `ValidationFinding` (or a manual-review request)
  into a `DecisionReason`.

Building the actual `DecisionReason` list from a real `InvoiceValidation` —
walking its findings, looking up each one's policy, appending the manual
reason when requested, and calling `InvoiceDecision.from_reasons` — is the
package 3 engine's job, not this package's. Package 1 fixes the data shapes
and the policy table those steps will use; it deliberately stops short of
reading a real validation attempt.

---

## Part 4 — Persistence and audit representation (package 2)

Two tables, mirroring `invoice_validations` / `invoice_validation_findings`
exactly in shape (`backend/app/models/decision.py`; migration
`backend/alembic/versions/0005_decision_tables.py`):

- `invoice_decisions` — one row per decision attempt, keyed
  `(validation_id, attempt_number)` with a partial unique index for one
  active `PROCESSING` attempt per source validation, mirroring Stage 4/5's
  attempt-history pattern exactly.
- `invoice_decision_reasons` — the ordered reasons for one attempt,
  `position`-ordered, mirroring `invoice_validation_findings`.

Three choices go beyond a mechanical copy of the Stage 5 shape, each recorded
here so a later package does not have to rediscover the reasoning:

- **`outcome` is a stored column, not re-derived.** Stage 5's `summary` is
  deliberately never stored (re-derived from `findings` on every read, so it
  cannot drift). Stage 6's `outcome` is different: it is the entire reason
  Stage 6 exists, and package 4/5 will need to query and filter by it
  directly (for example, listing every document that needs review) without
  loading and re-deriving from every reason row of every attempt. It still
  cannot drift from `reasons` in practice, because
  `app.schemas.decision.InvoiceDecision` refuses to construct an
  inconsistent pair in the first place (§1.7) — the repository simply trusts
  that guarantee rather than re-checking it.
- **`source_finding_id` is the literal "finding reference" the handoff asks
  for.** Each rule-derived `invoice_decision_reasons` row carries a real
  foreign key to the exact `invoice_validation_findings` row it explains (not
  just the rule name), so a reason's full `expected` / `actual` / `context`
  stays reachable without ever being copied onto the reason. It is `NULL`
  exactly for `manual_review_requested`, which has no finding behind it. A
  single CHECK (`source_rule_matches_code`) ties `source_rule`,
  `source_finding_id`, and `code` together so all three can only ever agree.
  The finding foreign key deliberately uses `NO ACTION`, not a reason-level
  delete cascade: removing a referenced finding by itself is rejected so it
  cannot silently change a completed decision's explanation while leaving its
  stored outcome behind. Deleting the owning validation or document still
  removes the entire derived decision branch through its parent cascade.
- **`source_rule` is plain, CHECK-constrained text, not a second native enum
  reusing Stage 5's `validation_rule` type.** The valid value set is already
  closed and enforced application-side by
  `app.schemas.decision.DecisionReason`, and the same combined CHECK proves a
  non-null `source_rule` can only ever be one of the fifteen rule codes (by
  requiring it to equal `code`, which is itself a closed enum) — a separate
  list-membership CHECK or a second native type sharing (and version-coupling)
  a type across two migrations would add nothing.
- **`policy_version` is stamped once, at attempt creation.** It names the
  `decision_catalogue.POLICY_VERSION` revision in effect when the attempt
  ran, independent of whether the attempt succeeds, so a decision can always
  be traced to the exact policy text that produced it even after §2.2 is
  later retuned. Bump `POLICY_VERSION` whenever a `triggers_review` value,
  the reason set, or a message changes.

Persistence schemas (`backend/app/schemas/decision_persistence.py`, mirroring
`validation_persistence.py`): `reason_rows(decision, source_finding_ids)`
flattens an `InvoiceDecision` into ordered rows, taking the finding-id
sequence as a second, position-aligned argument since that reference is
persistence-only and does not live on the pure contract;
`invoice_decision_from_rows(rows)` rebuilds the contract (re-validating the
closed code enum, the `source_rule` / `code` agreement, the `field_path`
shape, and the re-derived `outcome`) from stored rows, ignoring
`source_finding_id` on the way back in.

`backend/app/services/processing/decision/repository.py`:
`DecisionRepository` is the sole reader/writer of both tables, mirroring
`ValidationRepository`'s read methods (`get`, `get_for_validation`,
`list_for_validation`, `latest_for_validation`, `active_for_validation`,
`next_attempt_number`) and its two-phase write shape (`add_attempt` stamps
`PROCESSING` + `policy_version` and stages the row; `apply_result` sets
`outcome` and replaces `reasons` from an already-built `InvoiceDecision`, and
does not itself change `status` or commit). It builds no `InvoiceDecision`
itself — that is the package 3 engine's job — and it never writes to any
Stage 2–5 row.

Verified directly against PostgreSQL: `alembic upgrade head` /
`downgrade -1` / `upgrade head` / `check` round-trips cleanly on a throwaway
database, and a seeded Stage 2–5 row chain plus one decision attempt and
reason survive the downgrade + re-upgrade with the decision tables emptied
and recreated, Stage 2–5 data byte-for-byte unchanged. A full automated
version of this check (mirroring `test_stage5_verification.py`) belongs to
package 6, alongside the rest of Stage 6's end-to-end verification.

---

## Part 5 — Deterministic decision engine (package 3)

`backend/app/services/processing/decision/engine.py` implements the §2.2
mapping as one pure function:

```python
decide(validation: InvoiceValidation, *, manual_review_requested: bool = False) -> InvoiceDecision
```

It takes no session and makes no database, AI, or network call — the whole
decision is a function of its two arguments plus the fixed
`decision_catalogue` table, matching §2.6 exactly. It reads only
`finding.rule` (to look up policy) and `finding.field_path` (carried through
unchanged); `severity`, `expected`, `actual`, and `context` are never
touched, so nothing Stage 5 recorded about *why* a finding fired leaks into,
or is needed by, the decision layer.

`manual_review_requested` is strict at this boundary: values such as `0`,
`1`, `null`, or the string `"false"` are rejected rather than interpreted by
Python truthiness. This matters because the flag can turn an otherwise
accepted invoice into `NEEDS_REVIEW`.

**The 1:1, order-preserving correspondence.** Every finding in
`validation.findings` becomes exactly one reason, via the exposed
`reason_for_finding(finding)`, in the same order; a manual-review request
appends exactly one more, via `manual_review_reason()`, last. No finding is
ever skipped, merged, or reordered — this is the mechanism that makes §2.1's
"no reason is discarded" and "no precedence among reasons" rules hold by
construction, not by convention. `reason_for_finding` and
`manual_review_reason` are exposed as their own functions (not just inlined
into `decide`) specifically so package 4's service can rebuild the parallel
`source_finding_ids` sequence `decision_persistence.reason_rows` needs: it
already holds the ORM `ValidationFindingRow` list (each with a
`validation_finding_id`) in the same order as `validation.findings`, so
zipping that against `decide`'s reasons (or against direct
`reason_for_finding` calls) is enough — no separate lookup or matching logic
is needed anywhere.

The outcome itself is not computed here at all: `decide` builds the reason
list and hands it to `InvoiceDecision.from_reasons`, so "`NEEDS_REVIEW` iff
any reason triggers review" is enforced once, as a contract invariant
(Part 3), not duplicated as engine logic that could drift from it.

Verified in `backend/tests/test_decision_engine.py`: every one of the 15
rules end to end against an independent transcription of the §2.2 table
(catching a wiring bug even if `decision_catalogue.py` itself were
internally consistent but wrong); field_path pass-through; that a reason's
message always comes from the fixed policy text, never a finding's own
`message`; conflicting findings (mixed gating and non-gating) producing every
reason with no reordering; the duplicate/high-value elevation and the
missing/low-confidence split from §2.2–2.3, including the realistic
all-five-critical-fields-unavailable OpenAI shape still accepting a clean
invoice; manual review alone, combined with findings, and appended last;
the 1:1 correspondence; determinism across equal-but-distinct inputs; and
that `decide` never mutates its input.

---

## Part 6 — Lifecycle, orchestration, and document status (package 4)

### 6.1 Decision-attempt lifecycle

Mirrors Stage 4/5 exactly, with the source being a `COMPLETED` validation
attempt (`backend/app/services/processing/decision/lifecycle.py`):

```
COMPLETED validation ──(start)──> PROCESSING ──> COMPLETED   (outcome stored)
                                          └────> FAILED        (technical only)
FAILED decision ──(explicit retry)──> PROCESSING ──> COMPLETED | FAILED
```

`ensure_validation_can_decide(validation_status, latest_decision_status, *,
action)` raises `ConflictError` (409):

- `VALIDATION_NOT_COMPLETED` — the source validation is not `COMPLETED`
  (still `PROCESSING`, or technically `FAILED`).
- `DECISION_IN_PROGRESS` — a decision attempt is already `PROCESSING`.
- `VALIDATION_ALREADY_DECIDED` — a `start` when a `COMPLETED` decision
  attempt already exists (terminal — no re-decide route, exactly like Stage 5
  has no re-validate route).
- `DECISION_FAILED` — a `start` when the latest attempt is a technical
  failure; retry it instead.
- `DECISION_NOT_FAILED` — a `retry` when the latest attempt is not a
  technical failure, or there is no attempt yet.

`ensure_attempt_transition` allows only `PROCESSING -> COMPLETED | FAILED`.
A `NEEDS_REVIEW` outcome completes the attempt exactly like `ACCEPTED` —
it is never a `FAILED` decision (§1.5).

### 6.2 The document-status mapping (pinned before implementation)

Stage 6 is the **first** stage to write `documents.status` since Stage 3 set
it to `COMPLETED` — Stage 4 and Stage 5 never touch it. The
`document_status` enum has carried a `NEEDS_REVIEW` member, unused, since the
Stage 2 migration; this package is what finally assigns it. No new document
status and no new migration are needed.

| Decision attempt outcome | `documents.status` write |
|---|---|
| `ACCEPTED` | **Unchanged.** Stays `COMPLETED` — a value that already means "extraction finished," not "accepted" (`app/services/processing/extraction/lifecycle.py`'s own docstring already says this). Whether a `COMPLETED` document was ever decided, and what it decided, is only knowable from its decision attempt(s), exactly as Stage 4/5 results are only knowable from their own attempt tables. |
| `NEEDS_REVIEW` | `documents.status = NEEDS_REVIEW`. |
| *(technical `FAILED` decision attempt)* | **Unchanged.** A failed decision attempt is an infrastructure blip, not a fact about the invoice — the underlying validation is still fully valid and complete. Flipping the document to a generic failure state would wrongly suggest the invoice data itself is bad. The document stays `COMPLETED` (awaiting a retry), the same way a technical Stage 4/5 failure changes nothing upstream. |

This is a one-way, one-time transition: `NEEDS_REVIEW` is written at most once
per document (a decision attempt only ever reaches `COMPLETED` once, same as
validation), and nothing in Stage 6 ever moves a document back to
`COMPLETED`. Approving or rejecting a `NEEDS_REVIEW` document is later,
separate work (§1.4, §1.6) and is not designed here.

### 6.3 Extraction guard compatibility (inspected, no change needed)

`ensure_document_can_extract` (`app/services/processing/extraction/lifecycle.py`)
allows `start` only from `UPLOADED` and `retry` only from `FAILED`.
`NEEDS_REVIEW` is in neither set, so both calls against a `NEEDS_REVIEW`
document are already rejected with no code change — a `start` falls through
to the generic `DOCUMENT_NOT_EXTRACTABLE`; a `retry` hits its own "not
`FAILED`" branch first and returns `EXTRACTION_NOT_FAILED` instead (a
different code, equally a `409`, since that branch is unconditional for any
non-`FAILED` status once `PROCESSING` is ruled out). Either way, a document
awaiting review cannot be silently re-extracted out from under its own
decision. Verified by inspection and by a package 4 test.

### 6.4 Preventing a stale source from overriding the current outcome

**The guard.** Before starting or retrying a decision attempt, the service
confirms the source validation is still part of the document's *current*
processing chain: its normalization's extraction must be the document's
latest extraction attempt, and its normalization must be that extraction's
latest normalization attempt. If not,
`ensure_validation_is_current_source(...)` raises `ConflictError` `409`
`STALE_VALIDATION_SOURCE` before any decision attempt row is created — Stage
6 never fabricates or overwrites a document outcome from a source that has
been superseded.

**Why this cannot happen today, and why the guard exists anyway.** Extraction
only retries from `FAILED` (never from `COMPLETED`), normalization only
starts against a `COMPLETED` extraction and only retries a technical
`FAILED` attempt, and validation only starts against a `COMPLETED`
normalization with no re-validate route. Chained together, these mean a
document can have at most one ever-`COMPLETED` extraction, normalization,
and validation — there is only ever one processing chain, so this guard's
`409` branch is not reachable through today's API. It is implemented anyway,
defensively, because it is cheap, it is exactly what the handoff asks for
("prevent an old source attempt from overwriting the current document
outcome"), and it stops silently producing a wrong document status the
moment any future stage adds a second chain (for example, a future
re-extraction-after-review capability) without anyone having to remember to
add this check retroactively. Package 6 tests it by constructing a second
chain directly against the database (bypassing the API, since the API cannot
produce one today) and confirming the guard fires.

### 6.5 Concurrency and locking

Mirrors Stage 4/5's `SELECT ... FOR UPDATE` + partial-unique-index pattern,
extended to cover the document row too (Stage 4/5 never needed this, since
neither touches `documents`):

- `_run` resolves and locks, in one query, the source `ValidationAttempt`
  row, the owning `Document` row, and the current chain's extraction/
  normalization ids (`FOR UPDATE OF` naming both tables — Postgres locks only
  those two rows, not every joined table) — serialising concurrent decision
  starts on the same validation and making the §6.4 staleness check
  read-consistent with the lifecycle check.
- The lock is released at commit, immediately after the `PROCESSING` attempt
  is durably written — exactly the "PROCESSING is durable before work
  starts" guarantee Stage 3/4/5 already give.
- The partial unique index on `invoice_decisions` (package 2) remains the
  hard backstop: a concurrent `start`/`retry` that slips past the lock (or
  arrives after it is released) still loses on `IntegrityError`, converted to
  `409 DECISION_IN_PROGRESS`.
- `_complete` re-locks the validation and document and repeats the §6.4
  current-chain check in the same transaction that persists the outcome and
  any `NEEDS_REVIEW` status. This closes the interval between the initial
  guard and completion: even if a future reprocessing feature permits a new
  chain while evaluation runs, an old source cannot overwrite its document
  outcome. Nothing else can start a second decision attempt for the same
  validation while this one is `PROCESSING` (the index).

### 6.6 Service orchestration

`backend/app/services/processing/decision/service.py`'s `DecisionService`
mirrors `ValidationService` step for step: `start` / `retry` call one
internal `_run`, which locks the source (§6.5), applies §6.1 and §6.4's
guards, checks for an already-active attempt, creates the `PROCESSING`
attempt (stamping `policy_version = decision_catalogue.POLICY_VERSION`),
flushes and commits. It then calls the package 3 `engine.decide` — pure, no
session — outside any transaction. `_complete` re-fetches the attempt and
the source validation's findings, calls `DecisionRepository.apply_result`
with the `source_finding_ids` built by zipping the validation's findings
against `decide`'s reasons (§Part 5), applies the §6.2 document-status
write, marks the attempt `COMPLETED`, and commits in one transaction. Any
fault anywhere in that sequence rolls back and falls through to
`_mark_failed`, which records a generic `DECISION_FAILED` failure against
the already-committed `PROCESSING` attempt and leaves `documents.status`
untouched (§6.2).

The service validates `manual_review_requested` as a strict boolean before it
locks the source or creates the durable `PROCESSING` row. An invalid internal
caller value is therefore a caller error, not a fabricated retryable
`DECISION_FAILED` attempt; the package 5 request schemas enforce the same rule
at the HTTP boundary.

---

## Part 7 — API and pipeline integration (package 5)

### 7.1 Scoped decision routes

`backend/app/api/decisions.py` adds five routes, every one hanging off a
completed Stage 5 validation attempt, mirroring the Stage 5 validation routes
one level deeper:

| Method | Path (under `/documents/{document_id}/extractions/{extraction_id}/normalizations/{normalization_id}/validations/{validation_id}/decisions`) | Purpose |
|---|---|---|
| `POST` | `` (base) | start the first decision (`201`) |
| `POST` | `/retry` | run a new attempt after a technical failure (`201`) |
| `GET`  | `` (base) | every attempt, newest first |
| `GET`  | `/latest` | the most recent attempt |
| `GET`  | `/{decision_id}` | one specific attempt, scoped to this validation |

`_validation_or_404` walks the full `document → extraction → normalization →
validation` chain and returns the first broken link as its own `404` code
(`DOCUMENT_NOT_FOUND`, `EXTRACTION_NOT_FOUND`, `NORMALIZATION_NOT_FOUND`,
`VALIDATION_NOT_FOUND`); an unknown decision id is `DECISION_NOT_FOUND`. The
`409` codes come straight from `lifecycle.ensure_validation_can_decide` and the
stale-source guard (§6.1, §6.4): `VALIDATION_NOT_COMPLETED`,
`DECISION_IN_PROGRESS`, `VALIDATION_ALREADY_DECIDED`, `DECISION_FAILED`,
`DECISION_NOT_FAILED`, `STALE_VALIDATION_SOURCE`. A decision that *runs* and
hits a technical fault is still `201` — the attempt row exists; its `status` is
`FAILED` and `failure_code` / `failure_message` (both client-safe) say why. A
`NEEDS_REVIEW` outcome is a normal `201` `COMPLETED` attempt.

`get_decision_service` (`backend/app/api/deps.py`) builds a `DecisionService`
bound to the request session — no provider, exactly like the normalization and
validation service dependencies.

### 7.2 Response and request schemas

`backend/app/schemas/decision_api.py`, mirroring `validation_api.py`:

- `DecisionStartRequest` — the body for `start` **and** `retry`. Its one field
  is `manual_review_requested: bool` (default `false`), declared `strict` so
  `0`, `1`, `"false"` and `null` are `422`, not coerced — the same strictness
  the engine boundary applies, because the flag can turn an otherwise accepted
  invoice into `NEEDS_REVIEW`. Unknown keys are rejected; an empty body is
  valid. A `retry` builds a brand-new attempt, so the flag must be supplied
  again to still apply.
- `InvoiceDecisionResult` — the public view of one attempt: `decision_id`,
  `validation_id`, `attempt_number`, `status`, `outcome`
  (`ACCEPTED | NEEDS_REVIEW | null`), `policy_version`, the four timestamps,
  `failure_code` / `failure_message`, and `data`. `data` is the full
  `InvoiceDecision` contract (`outcome` + ordered `reasons`) **only on a
  `COMPLETED` attempt**; it is `null` while `PROCESSING` and on a `FAILED`
  attempt, which has no business outcome at all (§1.5) — a failed decision is
  never `ACCEPTED`. A model validator enforces the status/`outcome`/`data`
  agreement (and `data.outcome == outcome` when both are present), mirroring
  the `_STATUS_FIELDS_CONSISTENT` CHECK on `invoice_decisions`. No
  approval/rejection field, no confidence, no raw payload, no other internal
  diagnostics.

### 7.3 `manual_review_requested` through the service

`DecisionService.start` / `retry` gain a keyword-only
`manual_review_requested: bool = False`, threaded straight to
`engine.decide(validation, manual_review_requested=...)`. `_evaluate` appends a
trailing `None` to its `source_finding_ids` list when the flag is set, so the
sequence stays position-aligned with `decide`'s reasons for
`decision_persistence.reason_rows` (the appended `manual_review_requested`
reason has no finding behind it — §Part 5). Nothing else in the service
changes: the flag only ever *adds* one reason, and since a `COMPLETED`
decision is terminal there is no path for it to revisit an outcome that
already exists (§2.4).

### 7.4 The pipeline's fourth stage

`ProcessingPipeline` now chains `extraction → normalization → validation →
decision` in one session. `_continue_to_decision(validation, *,
manual_review_requested)` runs `DecisionService.start` **only** when the
validation ended `COMPLETED`; a missing or `FAILED` validation leaves
`PipelineResult.decision = None`, exactly as a missing/failed normalization
leaves `validation = None` (§2.5). A `ConflictError` whose code is
`DECISION_IN_PROGRESS`, `VALIDATION_ALREADY_DECIDED`, or `DECISION_FAILED`
(for example a decision already started on the decision endpoint) is caught and
the latest attempt returned rather than forcing a second one — the same
defensive shape the normalization and validation continuations already use;
any other conflict propagates. A decision *technical* failure surfaces as
`decision.status == "FAILED"` in the response, not a pipeline error.

`PipelineRunRequest` gains the same strict `manual_review_requested` field,
forwarded to the decision stage and ignored when the chain stops earlier.
`PipelineRunResult` gains `decision: InvoiceDecisionResult | null`. The
per-stage endpoints and every existing pipeline response field are unchanged;
the composed response is still just the per-stage public results side by side.
No decision *policy* runs in the pipeline or the routes — it stays entirely in
the decision subsystem.

### 7.5 Manual-review input scope (resolves the §2.4 open question)

The `manual_review_requested` flag is accepted on `POST .../decisions`,
`POST .../decisions/retry`, and `POST /documents/{id}/pipeline[/retry]`. It is
add-only in every case (§2.4): there is no "force accept", and it cannot be
used to change a decision that has already `COMPLETED` (that route returns
`VALIDATION_ALREADY_DECIDED`). Who may set it is an authorization question,
and LedgerDrop has no authentication layer yet (`CLAUDE.md`); when one is
added, this flag is one of the inputs it will need to gate.

---

## Implementation order

See `CLAUDE.md`'s Stage 6 handoff for the full six-package plan and scope
limits. Summary of status:

1. **Boundary, decision policy, and contracts.** *(Done — this document, plus
   `app/schemas/decision.py` and `app/schemas/decision_catalogue.py` with
   their tests.)*
2. **Persistence, migration, and audit representation.** *(Done — Part 4
   above, plus `app/models/decision.py`, migration `0005_decision_tables`,
   `app/schemas/decision_persistence.py`, and
   `app/services/processing/decision/repository.py`, each with tests
   (`tests/test_decision_{model,persistence,repository}.py`).)*
3. **Deterministic decision engine.** *(Done — Part 5 above,
   `app/services/processing/decision/engine.py`, with
   `tests/test_decision_engine.py`.)*
4. **Lifecycle, orchestration, and document status.** *(Done — Part 6 above,
   `app/services/processing/decision/{lifecycle,service}.py`, with
   `tests/test_decision_{lifecycle,service}.py`.)*
5. **API and pipeline integration.** *(Done — Part 7 above,
   `app/api/decisions.py`, `app/schemas/decision_api.py`, the
   `DecisionService` / `ProcessingPipeline` / `pipeline_api` changes, with
   `tests/test_decisions_api.py` and additions to
   `tests/test_pipeline{,_api}.py`.)*
6. **End-to-end verification and documentation.** *(Done — "## Verification"
   below, `tests/test_stage6_verification.py`, and the status/README updates.)*

## Verification

`backend/tests/test_stage6_verification.py` is the executable pass over this
checklist. Like the Stage 4/5 verification suites, it drives scenarios through
the *composed stack* (upload → extraction → normalization → validation →
decision) rather than re-deriving matrices that already have a dedicated test
file, and it fills the bullets that had no automated coverage yet.

| Checklist item | Where it is proven |
|---|---|
| Clean invoice → `ACCEPTED`, document stays `COMPLETED`, `policy_version` stamped | `test_stage6_verification.py::test_clean_invoice_is_accepted_end_to_end` (+ `test_decision_service.py::test_start_decides_a_clean_invoice_as_accepted`) |
| All 15 rules → catalogued outcome, determinism, 1:1 order-preserving reasons | `test_decision_engine.py` (independent transcription of §2.2); re-affirmed through the stack by `test_stage6_verification.py::test_many_findings_produce_ordered_gated_reasons_end_to_end` |
| Multiple findings → every reason kept, Stage 5 order, `triggers_review` stored per reason, message reused verbatim | `test_stage6_verification.py::test_many_findings_produce_ordered_gated_reasons_end_to_end` |
| `high_value_invoice` elevated above its Stage 5 `info` severity (§2.2) | `test_stage6_verification.py::test_high_value_clean_invoice_is_elevated_to_review_end_to_end` |
| Duplicate invoice → review; A/B asymmetry | `test_stage6_verification.py::test_duplicate_invoice_is_routed_to_review_end_to_end` |
| Unavailable per-field confidence, every field `null` (the real GPT-5-mini shape) → still `ACCEPTED`, reasons kept, non-gating (§2.3) | `test_stage6_verification.py::test_all_null_confidence_invoice_is_still_accepted_end_to_end` (+ `test_decision_engine.py`) |
| Manual-review request → one gating reason appended last; add-only, cannot suppress a rule reason (§2.4) | `test_stage6_verification.py::test_manual_review_request_adds_a_reason_end_to_end` / `test_manual_review_does_not_suppress_a_rule_reason` |
| Upstream failure / not-yet-`COMPLETED` validation → no decision row, pipeline `decision` is `null` (§2.5) | `test_stage6_verification.py::test_pipeline_stops_before_decision_when_{extraction,validation}_fails` / `test_decision_start_rejected_when_source_validation_not_completed` |
| Technical decision failure → `FAILED` attempt, client-safe message, no partial reasons, document untouched, retryable | `test_stage6_verification.py::test_technical_failure_is_safe_and_retryable_end_to_end` (+ `test_decision_service.py`) |
| Retry a technical failure → attempt 2; a `COMPLETED` decision is terminal | `test_stage6_verification.py::test_technical_failure_is_safe_and_retryable_end_to_end` / `test_a_completed_decision_is_terminal` |
| One active attempt per validation; concurrent starts → exactly one wins | `test_stage6_verification.py::test_concurrent_decision_starts_at_the_api_level_only_one_wins` (+ `test_decision_service.py::test_two_concurrent_starts_only_one_wins`) |
| Stale-source guard fires when the source chain is superseded (§6.4) | `test_stage6_verification.py::test_a_superseded_validation_chain_is_rejected_as_stale` (+ `test_decision_service.py`) |
| Every Stage 2–5 row + the stored PDF unchanged after a decision run; only `documents.status` may change, and only to `NEEDS_REVIEW` | `test_stage6_verification.py::test_accepted_decision_leaves_the_whole_chain_and_pdf_untouched` / `test_needs_review_decision_changes_only_the_document_status` / `test_technical_failure_is_safe_and_retryable_end_to_end` |
| Retrieval + `404` / `409` across all five endpoints, ownership/lineage | `test_decisions_api.py` (26 tests) |
| DB relationships & constraints | `test_decision_model.py` |
| Migration `0005_decision_tables` upgrade/downgrade with Stage 2–5 data preserved byte-for-byte, `alembic check` clean at head | `test_stage6_verification.py::test_migration_upgrade_downgrade_preserves_stage2_5_data` |
| No AI call, no external-network call | `test_stage6_verification.py::test_decision_subsystem_source_has_no_ai_or_network_import` / `test_decide_makes_no_socket_connection` / `test_decision_schema_layer_imports_no_ai_sdk` |

**Result.** The whole backend suite passes, including the new
`test_stage6_verification.py` (20 tests) and the additions to
`test_pipeline{,_api}.py`. The `0005_decision_tables` migration round-trips
cleanly on a throwaway PostgreSQL database with every Stage 2–5 row preserved
byte-for-byte and `alembic check` clean at head. An `ACCEPTED` decision writes
nothing outside the two decision tables; a `NEEDS_REVIEW` decision additionally
writes exactly `documents.status` (`COMPLETED → NEEDS_REVIEW`) and its
`updated_at`, and nothing else on any Stage 2–5 row or the stored PDF; a
technical `FAILED` decision writes only its own attempt row.

## Open questions for the reviewer

- **`high_value_invoice` elevated to gating (§2.2).** Stage 5 scores it
  `info` (not a data defect); this document gates on it anyway as a business
  dual-control measure. Confirm this is the intended policy, or that it
  should instead stay non-gating like Stage 5's other `info` rules.
- **`critical_field_confidence_unavailable` stays non-gating (§2.3).** This is
  the call with the largest practical effect, since it currently applies to
  every invoice from the real provider. Confirm the reasoning in §2.3 (rely
  on the other deterministic checks instead) matches the business's risk
  tolerance, especially before higher transaction volumes or higher-value
  invoices lean on it.
- **`line_item_sum_not_checked` stays non-gating (§2.2).** Confirm that
  relying on the per-line `line_item_amount_mismatch` check is sufficient
  coverage, given the aggregate check could not run.
- **Manual-review request scope (§2.4).** *Resolved by package 5 (§7.5):* the
  flag is accepted on `POST .../decisions`, `.../decisions/retry`, and
  `POST /documents/{id}/pipeline[/retry]`, add-only in every case, and cannot
  revisit an already-`COMPLETED` decision. Who is authorized to set it remains
  an open authorization question, deferred until LedgerDrop has an
  authentication layer (`CLAUDE.md`).
