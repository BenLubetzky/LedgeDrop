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
mapping), the persistence, engine, and lifecycle/document-status design, the
full six-package implementation plan, and the verification map are all in
`docs/stage-6-decision.md`. Code lives under
`backend/app/services/processing/decision/` (`engine.py` is a pure
`decide(validation, *, manual_review_requested) -> InvoiceDecision` with no
session/AI/network call; `lifecycle.py`/`repository.py`/`service.py` mirror the
Stage 5 shape and additionally lock and write the owning `documents` row),
`backend/app/models/decision.py` (migration `0005_decision_tables`),
`backend/app/schemas/decision*.py`, and `backend/app/api/decisions.py`
(start/retry/list/latest/specific routes under
`.../validations/{vid}/decisions`). `DecisionService` writes `NEEDS_REVIEW` on
that outcome and leaves `documents.status` untouched on `ACCEPTED` or on a
technical failure; a review outcome is a successful decision, not a `FAILED`
attempt. GPT-5-mini's all-`null` per-field confidence is non-gating by pinned
policy and is never read as high confidence. `manual_review_requested` is an
add-only flag (on the decision routes and `POST /documents/{id}/pipeline`) that
cannot revisit a `COMPLETED` decision. `ProcessingPipeline` runs the decision
as a fourth stage only on a `COMPLETED` validation, leaving `decision=null`
otherwise; each stage stays independently callable.

**Scope limits:** backend decision/routing only. A review outcome prepares the
Stage 7 review workflow; it does not build review screens, editing,
notifications, human approval/rejection, or downstream posting. Any read-only
frontend work should be separately requested.

## Stage 7 (human review): complete

**Status: complete — all 5 packages done.** Stage 7 records a human resolution
of a `COMPLETED` Stage 6 decision whose outcome is `NEEDS_REVIEW`: `APPROVE` or
`REJECT`, attributed and timestamped, moving the document to a new terminal
`documents.status` (`APPROVED` / `REJECTED`, reachable only from `NEEDS_REVIEW`
and only here — `COMPLETED` keeps its "extraction finished" meaning). It
re-computes nothing upstream, never mutates a Stage 2–6 row or the stored PDF,
cannot override a Stage 6 `ACCEPTED` result, and makes no AI or network call. A
review is a **single terminal event** — no `PROCESSING`/`FAILED` row, no
`attempt_number`, no retry route; a technical failure persists nothing (submit
again), a success is terminal. Because LedgerDrop has no auth yet, the reviewer
name is an explicitly *unverified* label. Pinned policy, contract, persistence,
lifecycle/service/API and reviewer-UI design, and the verification map are in
`docs/stage-7-review.md`.

Backend code: `backend/app/schemas/review*.py`, `backend/app/models/review.py`
(`ReviewRecord` / `invoice_reviews`, one row per decision, `UNIQUE(decision_id)`;
migration `0006_review_tables` rebuilds the `document_status` enum by
rename-swap so it round-trips), `backend/app/services/processing/review/`
(`lifecycle.py` guards `DECISION_NOT_REVIEWABLE` / `DECISION_ALREADY_REVIEWED` /
`STALE_DECISION_SOURCE`; `service.py` `ReviewService.submit` writes the review
row + the document status in one transaction under a `SELECT ... FOR UPDATE` on
the target `invoice_decisions` + `documents` rows),
`backend/app/api/reviews.py` (`GET /reviews/queue` FIFO, `GET /reviews/{id}`,
deep-nested `POST`/`GET .../decisions/{did}/review`), `get_review_service` in
`deps.py`. Frontend: `frontend/src/app/review/` +
`src/components/review-{queue,detail}.tsx` + shared `src/lib/{api,review-types}.ts`
— the PDF beside a Field/Extracted/Normalized table, findings, decision reasons,
and an approve/reject form, rendering only client-safe fields.
`backend/tests/test_review_*.py`, `test_reviews_api.py`, and
`test_stage7_verification.py` (16 tests); full backend suite green.
`npm run lint` clean and `npx next build --webpack` compiles (Turbopack's
native SWC binary is blocked by Smart App Control on this machine — same
constraint as the Python toolchain).

**Scope limits:** queue, inspect, approve/reject, and audit trail only.
Notifications, authentication/organizations, and downstream posting remain
later-stage work.

## Stage 8 (reviewer corrections and re-decision): complete

Stage 8 records a **human correction** of a document's canonical invoice data,
re-runs the deterministic stages against the corrected values, and resolves the
document's status from the new outcome. Full spec — boundary, the four pinned
policies, persistence, the merge projection, the lifecycle/status matrix, the
API, the reviewer UI, and the verification map — is in
`docs/stage-8-corrections.md`.

**Pinned policy (project owner).** (1) Corrections are a **separate
append-only store** (`invoice_corrections` / `invoice_correction_fields`) with a
`base ⊕ corrections` merge projection; no Stage 2–7 row or the stored PDF is
ever mutated — the corrected canonical values are persisted as a **new**
`invoice_normalizations` attempt (tagged `source_correction_id`) beside the
immutable Stage 4 engine attempt. The pre-Stage-8 `/documents/{id}/corrections`
shortcut, which wrote a fake `human-correction` extraction attempt, is removed.
(2) A document is correctable from `NEEDS_REVIEW`, `COMPLETED`, `APPROVED`, or
`REJECTED`. (3) A re-decision of `ACCEPTED` **auto-accepts** — to `APPROVED`
from `NEEDS_REVIEW` / `APPROVED` / `REJECTED`, staying `COMPLETED` from
`COMPLETED`; a re-decision of `NEEDS_REVIEW` returns the document to the review
queue (against the new decision attempt). `documents.status` gains no new
value. (4) Every correction spawns a **new attempt chain**
(normalization → validation → decision), all attempts kept; the terminal
outcome is always the latest decision attempt plus any Stage 7 review on it.

**Code.** `backend/app/schemas/correction*.py`,
`backend/app/models/correction.py` (`CorrectionAttempt` / `CorrectionFieldRow`;
migration `0008_correction_tables`; adds
`invoice_normalizations.source_correction_id`),
`backend/app/services/processing/correction/` (`projection.py` is the pure
`base ⊕ corrections` merge reusing the Stage 4 field normalizers;
`lifecycle.py` / `repository.py` / `service.py` mirror the Stage 5–7 shape and
additionally lock and write the owning `documents` row),
`backend/app/api/corrections.py` (submit / retry / list / latest / specific
routes under `/documents/{id}/corrections`), `get_correction_service` in
`deps.py`. Frontend: the Stage 7 review-detail editor now targets the new
endpoint, renders a before/after diff from `entries`, and supports line-item
add/remove.

**Scope limits:** corrections, re-projection, re-validation, re-decision, and
the auto-accept/return-to-review resolution only. Line-item re-ordering (as
distinct from add/remove + edit), notifications, reviewer assignment,
authentication, and downstream posting remain later-stage work.

## Stage 9 (deployment readiness and MVP hardening): operational acceptance pending

**Status: the Packages 1–9 implementation is present; what remains is
operator execution against live infrastructure (a Render account + Cloudflare
R2) — provisioning, the local→R2 data cutover, a tested backup restore, and the
release + rollback rehearsals.** Stage 9 adds **no** document-processing
feature, changes **no** Stage 2–8 API/schema/lifecycle contract, and adds
**no** authentication. Full spec, per-package status, completion gate,
verification map, and the ⚠ provisional-values register:
`docs/stage-9-deployment-readiness.md`. Operator procedures:
`docs/stage-9-runbook.md`.

**In-repo deliverables.** `render.yaml` Blueprint (two isolated environments).
Production settings + `DATABASE_URL` scheme coercion + fail-fast
`check_deployment_safety` (a post-model `DeploymentConfigError`, not a pydantic
validator, so no secret reaches a traceback) + `.env.production.example` files.
`backend/Dockerfile` + `frontend/Dockerfile` (non-root, `output: "standalone"`)
+ `.dockerignore` × 2 + `docker-compose.prod.yml`. `FileStorage` ABC with
`LocalFileStorage` + `S3FileStorage` (aioboto3/R2) behind `build_storage`; the
read path uses `get_bytes` (no filesystem path leaves storage);
`backend/scripts/migrate_uploads_to_s3.py` cutover script; DB pool bounds.
Edge hardening in `app/core/middleware.py` + `rate_limit.py`: request-id,
security headers, body cap, `TrustedHostMiddleware`, narrowed CORS, `slowapi`
(off by default), `/docs` off in production. Observability in
`app/core/observability.py`: JSON logs with `request_id`, Sentry hook,
`/health` vs `/health/ready` (+ storage probe), `/metrics` +
`ledgerdrop_uploads_total`, pipeline stage duration/failure/outcome metrics,
and the review-queue-depth gauge. Guarded interrupted-worker recovery is in
`backend/scripts/recover_stuck_attempts.py`. `docs/stage-9-runbook.md` +
`backend/scripts/staging_smoke.py`.

**Pinned policy (project owner).** (1) **Platform: Render** — managed
PostgreSQL, Docker web services, per-environment configuration groups,
service-scoped prompted secrets, `render.yaml`
Blueprint IaC. (2) **Durable object storage: Cloudflare R2** (S3 API) via a new
`S3FileStorage` behind the storage interface; `LocalFileStorage` stays the
development/test default. (3) **Environments: staging and production**, fully
isolated (own database, own R2 bucket, own keys); staging exists for the
Package 8 end-to-end verification and the Package 9 release/rollback
rehearsals. (4) Still the invoice MVP — auth, orgs/tenancy, notifications,
reviewer assignment, downstream posting, line-item re-ordering, and non-PDF
input stay out of scope; operational security to deploy safely is in scope,
an identity/permissions product is not.

**Operator-owned areas** (recorded in the spec's per-package status): alert
rules are configured in Render/Sentry and release, rollback, restore, and
interrupted-worker drills run against live infrastructure. New backend dependencies:
`aioboto3`, `slowapi`, `sentry-sdk`, `prometheus-fastapi-instrumentator`
(runtime); `moto[s3,server]` (dev).

The completion gate and the full out-of-scope list are in the spec.

## Technology decisions

- Frontend: Next.js with TypeScript
- Backend: Python with FastAPI
- ORM: SQLAlchemy
- Database: PostgreSQL only; do not use SQLite
- Database migrations: Alembic
- Development file storage: local filesystem
- Production object storage: Cloudflare R2 (S3 API), pinned in Stage 9 with
  Render as the hosting platform; `S3FileStorage` behind the storage interface
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
       |-- Decision / escalation  <- Stage 6 (done)
       |-- Human review           <- Stage 7 (done)
       `-- Reviewer corrections   <- Stage 8 (done)
```

Stage 8 re-runs normalization (as a merge projection, not the Stage 4 engine),
validation, and the decision against `base ⊕ corrections`, then resolves
`documents.status`.

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

Development/base: `DATABASE_URL`, `UPLOAD_DIRECTORY`, `MAX_FILE_SIZE_MB=20`,
`MAX_PDF_PAGES=10`, `EXTRACTION_PROVIDER=fake|openai` (plus the OpenAI API key
when `openai` is selected). Stage 9 adds `ENVIRONMENT` (now
`development|test|staging|production`), `LOG_FORMAT=text|json`, `TRUSTED_HOSTS`,
`STORAGE_BACKEND=local|s3` with `S3_BUCKET` / `S3_ENDPOINT_URL` / `S3_REGION` /
`S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` / `S3_KEY_PREFIX`,
`RATE_LIMIT_ENABLED` / `RATE_LIMIT_DEFAULT` / `RATE_LIMIT_UPLOAD`, and
`SENTRY_DSN`. A `staging`/`production` process fails to boot unless the
configuration is deploy-safe (see `check_deployment_safety`). Keep safe
placeholders in `.env.example` and `.env.production.example` (backend and
frontend); never commit credentials or machine-specific values.

## Explicitly excluded until later stages

Do not implement these unless the user explicitly adds them to the current
stage's scope:

- Discarding uncertain fields (Stage 5 confidence findings are implemented)
- Defaulting missing currency or converting currencies
- Line-item **re-ordering** as a distinct correction operation (Stage 8
  supports add/remove + edit only)
- Reviewer notifications or assignment workflows beyond the Stage 7 MVP queue
- Downstream business-database integration
- Authentication, organizations, or multi-tenancy
- Images or non-PDF upload support
- Autonomous or multi-agent processing

Deterministic validation, reconciliation, confidence checks, and duplicate /
high-value findings are implemented in Stage 5. Decision and escalation work,
including `NEEDS_REVIEW`, is implemented in Stage 6. The queue, review view,
human approval/rejection, and audit trail are implemented in Stage 7. Editable
field corrections with re-projection, re-validation, re-decision, and the
auto-accept / return-to-review resolution are Stage 8 (complete). Deployment
readiness, durable production storage, operational security, observability,
recovery, and release verification are Stage 9 (implementation complete;
operator execution against live Render + R2 infrastructure remains).

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
