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

**Stage 2 (upload foundation) is complete.**

- FastAPI backend: environment-based config, async PostgreSQL, SQLAlchemy,
  Alembic, consistent API errors, health endpoints, local file storage.
- Endpoints: `POST /documents`, `GET /documents`, `GET /documents/{id}`,
  `GET /documents/{id}/file`.
- Server-authoritative PDF validation (one PDF per request, readable content,
  ≤ 20 MB, ≤ 10 pages), SHA-256 hashing, atomic storage, PostgreSQL metadata,
  cleanup when storage or database persistence fails.
- Next.js + TypeScript frontend: drag/drop upload, client-side PDF and size
  checks, progress and feedback, document list with statuses, links to view
  originals.
- Backend test suite green (41 tests at the Stage 2 checkpoint).

Accepted documents stay in `UPLOADED` until real processing begins.

**The authorized next stage is Stage 3: structured invoice extraction** —
convert a stored PDF into schema-constrained invoice data with a per-field
confidence value. Stage 3 does not implement normalization, business validation,
escalation decisions, or human review. The full specification (field contract,
persistence columns, preprocessing steps, provider-evaluation criteria, API
paths, required tests) is in `docs/stage-3-extraction.md`; read it before
implementing.

Stage 3 progress: step 1 (extraction data contract) is complete —
`backend/app/schemas/extraction.py`, tested in
`backend/tests/test_extraction_contract.py`.

## Technology decisions

- Frontend: Next.js with TypeScript
- Backend: Python with FastAPI
- ORM: SQLAlchemy
- Database: PostgreSQL only; do not use SQLite
- Database migrations: Alembic
- Development file storage: local filesystem
- Production object storage: deferred
- Overall architecture: modular monolith, not microservices
- AI/extraction provider: not selected yet; make this decision before provider
  integration and keep it behind a narrow interface
- Monetary and quantity values: decimal arithmetic, never binary floating point

## Architectural overview

```text
Browser -> Next.js frontend -> FastAPI document API
   |-- PostgreSQL metadata and extraction records
   |-- Local original-PDF storage
   `-- Processing pipeline
       |-- Extraction             <- Stage 3
       |-- Normalization          <- later
       |-- Validation             <- later
       `-- Decision / escalation  <- later
```

Extraction must be a separate backend subsystem. Provider-specific OCR, vision,
or LLM responses must not leak into API, database, normalization, or validation
contracts.

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

Stage 3 may determine that a readable PDF is not an invoice, is not in English,
or cannot be extracted. Do not broaden input support to images or other file
types during this stage.

## Stage 3 guardrails (summary — full spec in `docs/stage-3-extraction.md`)

- Lifecycle: `UPLOADED -> PROCESSING -> COMPLETED | FAILED`, and
  `FAILED -> PROCESSING -> COMPLETED | FAILED` on explicit retry. Do not set
  `PROCESSING` until extraction actually begins. `COMPLETED` means extraction
  finished only — not normalization or validation. `NEEDS_REVIEW` is not a
  Stage 3 status and must not be assigned for low confidence.
- One active extraction per document; reject duplicate or concurrent starts.
  A failure must leave the document and original PDF intact.
- Provider stays behind a small interface (input: provider-ready content;
  output: the validated internal contract). No application code depends on a
  provider SDK. Document the provider choice before building the real adapter.
- Every provider response is parsed and validated against the invoice schema
  before persistence; malformed output is never stored as a success. Raw
  provider responses are internal audit data and are never returned by public
  endpoints.
- Per-field confidence is a decimal in `[0, 1]` or `null`; there is no
  document-level confidence. Missing values are `null`, never invented.
- Money and quantities are decimals and must serialize without floating-point
  artifacts.
- Date values stay raw strings during extraction — not interpreted, reformatted,
  or rejected here. Currency is the conventional 3-letter alphabetic form,
  upper-cased. Neither is converted, defaulted, or recognized until
  normalization.
- Every scalar field, both keys of every `{value, confidence}` pair, and
  `line_items` must be present in a payload. Values and confidences may be
  `null`; later stages judge their meaning.
- Schema changes go through Alembic migrations.
- New API: start extraction, get latest result with per-field confidence, retry
  a failed extraction, `404` for unknown IDs, a clear conflict response for
  illegal status transitions. Stage 2 endpoints stay backward compatible.
- Frontend: show real processing status and, at most, a read-only extraction
  result. No editable human-review workflow.
- Automated tests use a deterministic fake provider and never call an external
  AI service.

## Stage 3 implementation order

Build in this sequence. Introduce no external AI/LLM call before step 12. Each
step is described in full in `docs/stage-3-extraction.md`.

1. Define the extraction data contract (schema only, no AI).
2. Design the extraction database models (`invoice_extractions`,
   `invoice_line_items`; multiple attempts per document).
3. Create the Alembic migration (verify upgrade + downgrade; Stage 2 data intact).
4. Create the backend schemas (internal / persistence / request / public).
5. Build the extraction repository/service foundation (transactional writes,
   attempt history, no conflicting active attempts).
6. Implement the processing lifecycle and explicit retry.
7. Add the extraction API endpoints (`404` + conflict responses; Stage 2 API
   unchanged).
8. Build and test PDF preprocessing (provider-independent; original untouched).
9. Create a deterministic fake extraction provider (no external calls).
10. Build an evaluation dataset.
11. Evaluate and select the real provider.
12. Integrate the real provider last (replaceable adapter behind the interface;
    schema-validate before persistence).
13. Run the complete Stage 3 verification.

Normalization follows Stage 3 and owns date interpretation, currency-code
recognition, and handling of invalid extracted values. Do not pull that forward.

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
`MAX_PDF_PAGES=10`. Add provider configuration only after selecting the provider.
Keep safe placeholders in `.env.example`; never commit credentials or
machine-specific values.

## Explicitly excluded from Stage 3

Do not implement these unless the user explicitly adds them to Stage 3 scope:

- Normalization rules beyond safe parsing into the extraction contract
- Defaulting missing currency to EUR or converting currencies
- Deterministic invoice validation and total reconciliation
- Confidence thresholds or discarding uncertain fields
- Duplicate-invoice decisions
- Escalation decisions and `NEEDS_REVIEW` transitions
- High-value invoice rules
- Human-review screens, editing, approval, or rejection
- Downstream business-database integration
- Authentication, organizations, or multi-tenancy
- Production object storage or deployment infrastructure
- Images or non-PDF upload support
- Autonomous or multi-agent processing

Deferred normalization, validation, confidence, and escalation context lives in
`docs/processing-spec.md`. Stage 3 may read it to keep contracts compatible but
must not implement its deferred decisions.

## Implementation conduct

- Stay within the authorized stage; if a decision materially expands scope,
  record the question and ask before implementing.
- Resolve the provider and API-contract decisions before coupling application
  code to an external service; keep provider behavior replaceable and
  deterministic in tests.
- Prefer straightforward, maintainable modules over speculative abstractions.
- Preserve clean boundaries between upload/storage, extraction, normalization,
  validation, and decisions.
- Keep public errors client-safe; never expose internal paths, secrets, raw
  provider payloads, or stack traces. Retain useful internal diagnostics.
- Update this document and the relevant READMEs when an agreed decision changes.
