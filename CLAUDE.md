# LedgerDrop

## Project purpose

LedgerDrop is a business-facing AI document-processing application intended to
reduce manual data entry. Users upload business documents; the application
extracts structured data, normalizes and validates it, and eventually either
accepts the result or sends it for human review.

The first supported use case is English-language invoices supplied as PDF files.
Keep the architecture extensible, but do not build abstractions for hypothetical
document types unless they directly help the invoice MVP.

## Current project state

**Stage 2 (upload foundation): complete.** FastAPI backend with environment
config, async PostgreSQL, SQLAlchemy, Alembic, consistent API errors, health
endpoints, and local file storage. Endpoints `POST /documents`, `GET /documents`,
`GET /documents/{id}`, `GET /documents/{id}/file`. Server-authoritative PDF
validation (one PDF per request, readable, ≤ 20 MB, ≤ 10 pages), SHA-256 hashing,
atomic storage, PostgreSQL metadata, cleanup on failure. Next.js + TypeScript
frontend with drag/drop upload, client-side checks, progress feedback, and a
document list. Accepted documents stay `UPLOADED` until processing runs.

**Stage 3 (structured invoice extraction): complete.** Converts a stored PDF
into schema-constrained invoice data with per-field confidence when the
provider supplies it. Full spec, implementation order, and code inventory:
`docs/stage-3-extraction.md`; provider rationale: `docs/provider-selection.md`.
Code lives under `backend/app/services/processing/extraction/`,
`backend/app/schemas/extraction*.py`, `backend/app/models/extraction.py`,
`backend/app/api/extractions.py`. The provider stays behind
`ExtractionProvider`; `FakeExtractionProvider` is the deterministic offline
default, `OpenAIExtractionProvider` (GPT-5-mini) is the real adapter behind
`EXTRACTION_PROVIDER=openai|fake`. GPT-5-mini supplies no calibrated per-field
confidence, so application confidence is `null` for every field.

**Stage 4 (normalization): complete.** Converts the raw, immutable output of a
completed Stage 3 extraction into a separate, traceable normalized result:
deterministic canonical values, with a structured field error wherever a value
is invalid or ambiguous. No AI or external-network call; no business
validation, confidence thresholds, reconciliation, or decisions. Full spec,
pinned policies, persistence layout, API, and verification map:
`docs/stage-4-normalization.md`. Code lives under
`backend/app/services/processing/normalization/` (normalizers, engine,
lifecycle, repository, service), `backend/app/models/normalization.py`
(migration `0003_normalization_tables`), `backend/app/schemas/normalization*.py`,
`backend/app/api/normalizations.py`. Each normalizer applies only the cleanup
its field policy permits (currency/numbers skip the broad text cleanup, so a
hidden control character becomes a field error, not a silent repair). A
field-level error never fails the attempt; only an engine or persistence
fault does, rolling back to `FAILED` with no partial result.

**Stage 5 (deterministic invoice validation): complete.** Consumes the exact
completed Stage 4 normalization attempt, evaluates a closed catalogue of 15
deterministic rules, and records structured findings. It reports **facts
only** — a rule violation completes validation with findings, not a technical
failure — and never decides acceptance, rejection, or escalation, and never
moves a document to `NEEDS_REVIEW` (that is Stage 6). No AI or
external-network call. Full spec — boundary, pinned policies (⚠ values are
provisional defaults pending review), rule catalogue, 14-step implementation
order, verification map: `docs/stage-5-validation.md`. Code lives under
`backend/app/services/processing/validation/` (`policy.py` vendors every ⚠
constant and lives nowhere else; `rules.py`/`engine.py`/`lifecycle.py`/
`repository.py`/`service.py` mirror the Stage 4 shape),
`backend/app/models/validation.py` (migration `0004_validation_tables`),
`backend/app/schemas/validation*.py`, `backend/app/api/validations.py`. A
`null` per-field confidence is never read as high/low — it always emits an
`info`-severity `critical_field_confidence_unavailable` finding, keeping rule
semantics deterministic and provider-independent. `ProcessingPipeline`
(`backend/app/services/processing/pipeline.py`) chains
`extraction -> normalization -> validation -> decision` (the decision stage
added in Stage 6 package 5) in one session; each stage stays independently
callable via its own endpoints.

## Stage 6 (decision and escalation): complete

**Status: complete — all 6 packages done.** Stage 6 consumes persisted
processing results and determines whether an invoice can be accepted or needs
human review. Full boundary, policy (including the complete 15-rule → decision-reason
mapping), contract, persistence design, engine design, and lifecycle/document-
status design are in `docs/stage-6-decision.md`. Package 1 code:
`backend/app/schemas/decision.py` + `decision_catalogue.py` (with
`backend/tests/test_decision_{contract,catalogue}.py`). Package 2 code:
`backend/app/models/decision.py`, migration `0005_decision_tables`,
`backend/app/schemas/decision_persistence.py`,
`backend/app/services/processing/decision/repository.py` (with
`backend/tests/test_decision_{model,persistence,repository}.py`); verified
against real PostgreSQL (`alembic upgrade head -> downgrade -1 -> upgrade
head -> check`, Stage 2–5 data preserved byte-for-byte). Package 3 code:
`backend/app/services/processing/decision/engine.py` — a pure
`decide(validation, *, manual_review_requested) -> InvoiceDecision`, no
session/AI/network call, with `backend/tests/test_decision_engine.py`.
Package 4 code: `backend/app/services/processing/decision/{lifecycle,
service}.py` — `DecisionService` mirrors `ValidationService` (source lock,
committed `PROCESSING`, generic `FAILED` on any technical fault, explicit
retry, one active attempt per validation) and additionally locks and writes
the owning `documents` row: `NEEDS_REVIEW` on that outcome, unchanged
(`COMPLETED`) on `ACCEPTED` or on a technical decision failure, and a
`STALE_VALIDATION_SOURCE` 409 if the validation is no longer the document's
current extraction/normalization chain (not reachable via today's API, kept
as a defensive guard) — with `backend/tests/test_decision_{lifecycle,
service}.py`. Package 5 code: `backend/app/api/decisions.py` (scoped
start/retry/list/latest/specific routes under
`.../validations/{vid}/decisions`), `backend/app/schemas/decision_api.py`
(`DecisionStartRequest` with a strict `manual_review_requested` bool;
`InvoiceDecisionResult` whose `data` is the `InvoiceDecision` only on a
`COMPLETED` attempt, `null` while `PROCESSING`/`FAILED`), `get_decision_service`
in `backend/app/api/deps.py`, `manual_review_requested` threaded through
`DecisionService.start`/`retry`, and a fourth pipeline stage
(`ProcessingPipeline._continue_to_decision`, `PipelineRunResult.decision`,
`PipelineRunRequest.manual_review_requested`) that runs the decision only on a
`COMPLETED` validation and leaves `decision=null` otherwise — with
`backend/tests/test_decisions_api.py` and additions to
`backend/tests/test_pipeline{,_api}.py`. Package 6 code:
`backend/tests/test_stage6_verification.py` — one executable pass over the
Stage 6 acceptance checklist through the composed stack (clean acceptance,
every review trigger incl. the `high_value_invoice` elevation, the all-`null`
confidence GPT-5-mini shape still accepting, manual review, upstream/technical
failures, retries, HTTP-level concurrency, and the stale-source guard), plus
the `0005_decision_tables` migration round trip, byte-for-byte source
immutability with only `documents.status` changing (and only to
`NEEDS_REVIEW`), and the decision-subsystem "no AI / no network" guards. Full
checklist → coverage map and the run result are in
`docs/stage-6-decision.md` "## Verification". The six-package plan below is
kept for history.

1. **Boundary, decision policy, and contracts.** Write
   `docs/stage-6-decision.md` before implementation. Define the input lineage,
   outcome/reason enums, public result shape, and a complete mapping of all 15
   Stage 5 rules to decision reasons and outcomes. Specify precedence when
   multiple findings apply, clean-invoice acceptance, manual-review requests,
   missing confidence, and failed/unusable upstream processing. Treat proposed
   business policies as provisional until agreed; do not infer acceptance from
   severity counts alone. In particular, GPT-5-mini supplies `null` confidence:
   decide explicitly whether unavailable critical-field confidence requires
   review, and never interpret it as high confidence. Reuse Stage 5 findings
   and thresholds rather than recalculating validation. Define technical attempt
   status separately from business outcome; automatic rejection and human
   approval/rejection remain outside this stage. Add contract/policy tests.
2. **Persistence, migration, and audit representation.** Add decision attempt
   and reason models, persistence schemas, repository, and an Alembic migration
   together. Preserve source attempt IDs, ordered reasons and their finding
   references, timestamps, and the policy version needed to explain an outcome.
   Decide how upstream failures without a completed validation are represented
   without fabricating a successful validation. Preserve history, enforce one
   active attempt per defined source, and verify migration upgrade/downgrade
   and lossless result round trips on PostgreSQL.
3. **Deterministic decision engine.** Implement the agreed mapping in
   `backend/app/services/processing/decision/`, with centralized policy and a
   pure evaluator. Produce stable outcomes and explainable reasons for clean
   invoices, conflicting findings, duplicate/high-value flags, missing or low
   confidence, manual review, and the agreed upstream-failure cases. No AI,
   external calls, invented confidence, discarded values, or mutation of
   extraction, normalization, or validation results. Test the full decision
   matrix and determinism.
4. **Lifecycle, orchestration, and document status.** Add service and lifecycle
   guards together: source locking, committed processing attempt, atomic final
   outcome/reasons, safe technical failure, explicit retry, concurrent-start
   protection, and preserved history. Pin the document-status mapping in the
   spec first: existing `COMPLETED` means extraction completion, not business
   acceptance. Integrate `NEEDS_REVIEW` deliberately and prevent an old source
   attempt from overwriting the current document outcome. Inspect extraction
   start/retry guards for compatibility. A review outcome is a successful
   decision, not a `FAILED` decision attempt. Test races, stale sources, retries,
   rollback, and document transitions.
5. **API and pipeline integration.** Add scoped start/retry/list/latest/specific
   decision routes, dependency wiring, and safe response/error schemas as one
   package. Extend the composed pipeline response with the decision result;
   implement the agreed stop/escalation behavior for failed upstream stages.
   Preserve independent stage endpoints and existing response fields. Keep
   policy inside the decision subsystem. Define manual-review input and its
   interaction with an existing outcome explicitly. Test ownership/lineage
   checks, `404`/`409` behavior, failed attempts, and the complete pipeline.
6. **End-to-end verification and documentation.** Verify clean acceptance,
   each review trigger, multiple reasons, unavailable confidence with the real
   provider's stored input shape, upstream failures, manual requests, retries,
   concurrency, and stale-source protection. Prove earlier stage results and
   original PDFs remain unchanged, and only the explicitly authorized document
   status fields change. Run relevant backend regression checks and migration
   checks; update this handoff, the stage spec, and READMEs with actual results.

**Scope limits:** backend decision/routing only. A review outcome prepares the
later review workflow; it does not build review screens, editing, notifications,
human approval/rejection, or downstream posting. Any read-only frontend work
should be separately requested.

## Technology decisions

- Frontend: Next.js with TypeScript
- Backend: Python with FastAPI
- ORM: SQLAlchemy
- Database: PostgreSQL only; do not use SQLite
- Database migrations: Alembic
- Development file storage: local filesystem
- Production object storage: deferred
- Overall architecture: modular monolith, not microservices
- AI/extraction provider: OpenAI GPT-5-mini is the current adapter, behind the
  `ExtractionProvider` interface. Azure AI Document Intelligence
  `prebuilt-invoice` is kept as a future migration candidate. Rationale and the
  superseded Azure/AWS bake-off plan are in `docs/provider-selection.md`.
- Monetary and quantity values: decimal arithmetic, never binary floating point

## Architectural overview

```text
Browser -> Next.js frontend -> FastAPI document API
   |-- PostgreSQL metadata and extraction records
   |-- Local original-PDF storage
   `-- Processing pipeline
       |-- Extraction             <- Stage 3 (done)
       |-- Normalization          <- Stage 4 (done)
       |-- Validation             <- Stage 5 (done)
       `-- Decision / escalation  <- Stage 6 (done)
```

Extraction and normalization are separate backend subsystems. Provider-specific
OCR, vision, or LLM responses must not leak into API, database, normalization,
or validation contracts.

## Directory structure

```text
LedgerDrop/
|-- frontend/ (public/, src/)
|-- backend/
|   |-- app/ (api/, core/, database/, models/, schemas/,
|   |         services/storage/, services/processing/{extraction,
|   |         normalization,validation,decision}/)
|   `-- tests/
|-- storage/uploads/
`-- docs/
```

Follow this structure unless a small conventional adjustment is necessary. Do
not reorganize the project without discussing it first.

## Input constraints

- PDF only; English-language invoices; one file per upload request
- Maximum file size: 20 MB; maximum page count: 10 pages
- Structurally readable PDF content is required

Do not broaden input support to images or other file types.

## Stage 3 invariants that must stay intact

- Lifecycle `UPLOADED -> PROCESSING -> COMPLETED | FAILED`, and
  `FAILED -> PROCESSING -> COMPLETED | FAILED` on explicit retry. `PROCESSING`
  is not set until extraction begins. `COMPLETED` means extraction finished
  only. `NEEDS_REVIEW` is not used.
- One active extraction per document; duplicate or concurrent starts are
  rejected. A failure leaves the document and original PDF intact.
- The provider stays behind `ExtractionProvider` (input: provider-ready
  content; output: the validated internal contract). No application code
  depends on a provider SDK.
- Every provider response is schema-validated before persistence; malformed
  output is never stored as a success. Raw provider responses are internal
  audit data and are never returned by public endpoints.
- Per-field confidence is a decimal in `[0, 1]` or `null`; there is no
  document-level confidence. Missing values are `null`, never invented.
- Money and quantities are decimals that serialize without floating-point
  artifacts. Extracted dates stay raw strings; currency is upper-cased but not
  recognized, converted, or defaulted (that is Stage 4).
- Every scalar field, both keys of every `{value, confidence}` pair, and
  `line_items` are present in a payload; values and confidences may be `null`.
- Automated tests use the deterministic fake provider and never call an
  external AI service.

## Stage 4 guardrails (summary — full spec in `docs/stage-4-normalization.md`)

- Stage 4 reads a completed Stage 3 extraction and produces a separate,
  traceable normalized result. Raw extraction values and the original PDF stay
  unchanged. Normalization is deterministic with no AI or external-network
  call.
- Valid dates become `YYYY-MM-DD`. Impossible or unrecognized dates produce an
  `invalid_date` error with a `null` value; a numeric date whose day/month
  order is not fixed by its digits is read day-first (`DD/MM/YYYY`) by default,
  not treated as an error.
- Currency codes are trimmed, upper-cased, and checked against an approved ISO
  4217 list. Symbols are interpreted only under explicit deterministic rules.
  Missing currency is not defaulted; foreign currency is not converted.
- Money and quantities use decimal arithmetic. Accepted grouping and decimal
  separators are documented; malformed or ambiguous numbers produce an error.
  Do not round unless an explicit precision rule requires it.
- Text may be trimmed and have repeated whitespace collapsed. Empty or
  whitespace-only values become `null`. Meaningful punctuation is preserved;
  invoice numbers and tax identifiers stay strings.
- A field-level normalization error is data, not a technical attempt failure.
  Store a stable field path, raw value, safe error code, and safe message. Only
  infrastructure failures make an attempt `FAILED`.
- Lifecycle `COMPLETED extraction -> PROCESSING -> COMPLETED | FAILED`, and
  `FAILED -> PROCESSING -> COMPLETED | FAILED` on explicit retry of a technical
  failure. At most one active normalization attempt per source extraction;
  attempt history is preserved; concurrent or illegal starts return `409`.
- Normalized records reference their source extraction attempt and never
  replace or mutate it. Schema changes go through Alembic migrations.
- New API: start normalization, get the latest or a specific normalized result,
  retry a failed attempt, `404` for unknown IDs, `409` for illegal transitions.
  Stage 2 and Stage 3 endpoints stay backward compatible.
- Public responses never expose internal exceptions, paths, secrets, or raw
  diagnostics.
- Exact date formats, numeric separator policies, currency-symbol rules,
  text-length limits, and invalid-value representation are decided and written
  into `docs/stage-4-normalization.md` before the step that needs them. Do not
  invent policy in code.
- Frontend: show normalization status and, at most, a read-only normalized
  result. No editable review workflow.
- Automated tests prove no AI or external-network call occurs.

## Stage 2 behavior that must stay intact

- `POST /documents` validates and stores one PDF and returns `UPLOADED` metadata.
- `GET /documents` lists safe metadata newest first;
  `GET /documents/{id}` returns one record;
  `GET /documents/{id}/file` safely streams the original PDF.
- Files stay at `storage/uploads/{document_id}/original.pdf`; the database stores
  only the relative reference. Internal paths and file hashes are never exposed.
- Upload failures leave no orphaned files or database rows.
- The backend remains authoritative for file type, integrity, size, and
  page-count validation.

## Configuration

Existing: `DATABASE_URL`, `UPLOAD_DIRECTORY`, `MAX_FILE_SIZE_MB=20`,
`MAX_PDF_PAGES=10`, `EXTRACTION_PROVIDER=fake|openai` (plus the OpenAI API key
when `openai` is selected). Add Stage 4 configuration only if a normalization
policy genuinely requires it. Keep safe placeholders in `.env.example`; never
commit credentials or machine-specific values.

## Explicitly excluded until later stages

Do not implement these unless the user explicitly adds them to the current
stage's scope:

- Discarding uncertain fields (Stage 5 confidence findings are implemented)
- Defaulting missing currency or converting currencies
- Human-review screens, editing, approval, or rejection
- Downstream business-database integration
- Authentication, organizations, or multi-tenancy
- Production object storage or deployment infrastructure
- Images or non-PDF upload support
- Autonomous or multi-agent processing

Deterministic validation, reconciliation, confidence checks, and duplicate /
high-value findings are already implemented in Stage 5. Decision and escalation
work, including `NEEDS_REVIEW`, belongs to the Stage 6 plan above.

## Implementation conduct

- Stay within the authorized stage; if a decision materially expands scope,
  record the question and ask before implementing.
- Keep provider behavior replaceable and deterministic in tests.
- Prefer straightforward, maintainable modules over speculative abstractions.
- Preserve clean boundaries between upload/storage, extraction, normalization,
  validation, and decisions.
- Keep public errors client-safe; never expose internal paths, secrets, raw
  provider payloads, or stack traces. Retain useful internal diagnostics.
- Update this document and the relevant READMEs when an agreed decision changes.
- Keep this file lean; detailed stage specs live in `docs/`.
