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
into schema-constrained invoice data with per-field confidence when the provider
supplies it. Full spec in `docs/stage-3-extraction.md`; provider rationale in
`docs/provider-selection.md`. Key code:

- Contract: `backend/app/schemas/extraction.py`
- Persistence: `backend/app/models/extraction.py`; migration
  `0002_invoice_extraction_tables`
- Schemas: `backend/app/schemas/extraction_persistence.py`, `extraction_api.py`
- Service/lifecycle: `backend/app/services/processing/extraction/`
  (`lifecycle.py`, `repository.py`, `service.py`); drives
  `UPLOADED|FAILED -> PROCESSING -> COMPLETED|FAILED`
- API: `backend/app/api/extractions.py` (`POST`/`GET` under
  `/documents/{id}/extractions[...]`)
- Preprocessing: `backend/app/services/processing/extraction/preprocessing.py`
- Provider boundary: `provider.py` (`ExtractionProvider` + `ProviderError`
  hierarchy); offline `FakeExtractionProvider` in `fake.py`; real adapter
  `OpenAIExtractionProvider` in `openai_provider.py` (OpenAI GPT-5-mini).
  `EXTRACTION_PROVIDER=openai|fake` selects it in `app/api/deps.py` (`fake` by
  default). GPT-5-mini supplies no calibrated per-field confidence, so
  application confidence is `null` for every field.
- Evaluation: `backend/evaluation/` (`expected.json`, `generate_invoices.py`,
  `dataset.py`, `scoring.py`)

**Stage 4 (normalization): authorized, current stage.** Stage 4 converts the
raw, immutable output of a completed Stage 3 extraction into separate canonical
values where normalization is deterministic, and records structured field errors
where a value is invalid or ambiguous. It makes no AI or external-network call.
It does not implement business validation, confidence thresholds, totals
reconciliation, duplicate decisions, acceptance/rejection, escalation, or human
review. Full specification — contract, policies to pin, persistence layout, API
scope, 14-step implementation order, verification list — is in
`docs/stage-4-normalization.md`; read it before implementing.

Stage 4 progress: steps 1–5 are complete. Step 1 — the internal normalized
invoice data contract in `backend/app/schemas/normalization.py`
(`NormalizedInvoice`, `NormalizedLineItem`, `NormalizationError` /
`NormalizationErrorCode`, `NormalizedInvoiceResult`, plus the `NormalizedDate` /
`NormalizedCurrencyCode` / `NormalizedText` leaf types); test
`test_normalization_contract.py`. Normalized fields hold a single canonical
value or `null` and carry no confidence; a structured error records
`field_path`, `raw_value`, `code`, and a client-safe `message`. Step 2 — the
normalization policies (dates, numbers, currency, currency symbols, whitespace,
empty values, text limits, failure behavior) are decided and written in
`docs/stage-4-normalization.md`; `NormalizationErrorCode` is finalized at six
members, and numeric dates with an undetermined day/month order are read
day-first (`DD/MM/YYYY`) by default rather than erroring. Money and quantities
arrive from Stage 3 already parsed to `Decimal`, so normalization preserves
sign and scale without rounding rather than re-parsing number text. Step 3 —
the persistence models in `backend/app/models/normalization.py`
(`NormalizationAttempt`, `NormalizationLineItem`, `NormalizationFieldError`,
plus the `NormalizationStatus` / `NormalizationErrorCode` enums): tables
`invoice_normalizations`, `invoice_normalized_line_items`,
`invoice_normalization_errors`, keyed `(extraction_id, attempt_number)` with a
partial unique index for one active attempt; `ExtractionAttempt` gains a
`normalizations` relationship; test `test_normalization_model.py`. Step 4 — the
Alembic migration `0003_normalization_tables` (down_revision
`0002_invoice_extraction_tables`) creates the two enum types and three tables
with all constraints/indexes and drops them on downgrade; verified
up/down/re-up on a throwaway database with seeded Stage 2/3 rows intact, and
`alembic check` reports no model drift. Step 5 — the schema layer:
`backend/app/schemas/normalization_persistence.py` (nested `NormalizedInvoice`
<-> flat column bridge, no `_value`/`_confidence` split) and
`normalization_api.py` (`NormalizationStartRequest`;
`InvoiceNormalizationResult.from_attempt`, exposing status, timestamps,
`extraction_id`, failure info and `data: NormalizedInvoice` — no confidence,
no diagnostics); test `test_normalization_schemas.py`.

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
       |-- Normalization          <- Stage 4 (current)
       |-- Validation             <- later
       `-- Decision / escalation  <- later
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

- Deterministic invoice validation and total/line-item reconciliation
- Confidence thresholds or discarding uncertain fields
- Defaulting missing currency or converting currencies
- Duplicate-invoice decisions
- Escalation decisions and `NEEDS_REVIEW` transitions
- High-value invoice rules
- Human-review screens, editing, approval, or rejection
- Downstream business-database integration
- Authentication, organizations, or multi-tenancy
- Production object storage or deployment infrastructure
- Images or non-PDF upload support
- Autonomous or multi-agent processing

Deferred validation, confidence, and escalation context lives in
`docs/processing-spec.md`. The current stage may read it to keep contracts
compatible but must not implement its deferred decisions.

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
