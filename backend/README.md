# LedgerDrop backend

FastAPI + async SQLAlchemy service. Stage 2 scope is the **upload foundation**:
the application skeleton, configuration, database layer, migrations, consistent
API errors, and a local file-storage service. Invoice extraction and later
processing stages are intentionally not implemented.

## Requirements

- [uv](https://docs.astral.sh/uv/) (manages the Python 3.12 toolchain and deps)
- Docker + Docker Compose (local PostgreSQL)

## Setup

```bash
# from the repository root
docker compose up -d db

cd backend
cp .env.example .env
uv sync
```

## Database migrations

```bash
# from backend/, with the database running
uv run alembic upgrade head          # apply all migrations
uv run alembic downgrade -1          # roll back the last one
uv run alembic revision --autogenerate -m "describe change"   # create a new one
```

Alembic reads `DATABASE_URL` from the environment (via `app.core.config`), so
there is a single source of truth for the connection string.

## Run the API

```bash
# from backend/
uv run uvicorn app.main:app --reload
```

- `GET /health` — liveness
- `GET /health/ready` — liveness + database connectivity
- `POST /documents` — upload one PDF (`multipart/form-data`, field `file`); validates
  extension + content, 20 MB / 10-page limits, hashes and stores it, creates the
  record, returns `201` with public metadata. A failed upload leaves neither a row
  nor a stored file.
- `GET /documents` — all document metadata, newest first (by `uploaded_at`).
- `GET /documents/{document_id}` — one document's metadata; `404 DOCUMENT_NOT_FOUND`
  if unknown, `422 VALIDATION_ERROR` if the id is not a UUID.
- `GET /documents/{document_id}/file` — streams the stored PDF
  (`Content-Type: application/pdf`, `Content-Disposition: inline; filename="<original>"`).
  `404 DOCUMENT_NOT_FOUND` if the record is unknown; `404 FILE_NOT_FOUND` if the
  record exists but the stored file is missing. The server's filesystem path is
  never exposed.
- Interactive docs at `http://localhost:8000/docs`

Public metadata (`DocumentRead`) is `document_id`, `original_filename`,
`file_size_bytes`, `page_count`, `status`, `uploaded_at`, `updated_at`.
`file_location` and `file_hash` are never returned by any endpoint.

### Upload error codes

| Status | `error.code` | Cause |
|--------|--------------|-------|
| 400 | `FILE_REQUIRED` / `EMPTY_FILE` | no file part, or zero bytes |
| 400 | `FILENAME_TOO_LONG` | original filename exceeds 512 characters |
| 413 | `FILE_TOO_LARGE` | body exceeds `MAX_FILE_SIZE_MB` |
| 415 | `NOT_A_PDF` | wrong extension, or missing `%PDF-` signature |
| 422 | `PDF_UNREADABLE` | signature present but structure unparseable |
| 422 | `TOO_MANY_PAGES` | exceeds `MAX_PDF_PAGES` |
| 422 | `VALIDATION_ERROR` | no `file` field in the request at all |

## Extraction API (Stage 3)

Stage 3 adds structured invoice extraction. The endpoints are live; they run on
a **deterministic offline fake provider** (`FakeExtractionProvider`,
`provider_name` `fake-deterministic`) until a real one is chosen and integrated.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/documents/{document_id}/extractions` | start the first extraction (`201`) |
| `POST` | `/documents/{document_id}/extractions/retry` | run a new attempt after a failure (`201`) |
| `GET`  | `/documents/{document_id}/extractions` | every attempt, newest first |
| `GET`  | `/documents/{document_id}/extractions/latest` | the most recent attempt |
| `GET`  | `/documents/{document_id}/extractions/{extraction_id}` | one specific attempt |

- `404 DOCUMENT_NOT_FOUND` for an unknown document; `404 EXTRACTION_NOT_FOUND`
  when a document has no matching attempt.
- `409` when the document cannot legally transition: `EXTRACTION_IN_PROGRESS`,
  `DOCUMENT_ALREADY_EXTRACTED` (a second `start`), `EXTRACTION_NOT_FAILED`
  (`retry` on a document that has not failed).
- An extraction that *runs* and fails is still `201` — the attempt was created;
  its `status` is `FAILED` and `failure_code` / `failure_message` say why. Use
  `retry` to try again.

**Request** (`ExtractionStartRequest`) — start and retry take an empty JSON body
(`{}` or none). There are no parameters yet (the provider is chosen by server
configuration); unknown keys are rejected with `422 VALIDATION_ERROR`.

**Response** (`InvoiceExtractionResult`):

```jsonc
{
  "extraction_id": "…", "document_id": "…", "attempt_number": 1,
  "status": "PROCESSING | COMPLETED | FAILED",
  "provider_name": "…", "provider_model": "…" | null,
  "started_at": "…Z", "completed_at": "…Z" | null,
  "created_at": "…Z", "updated_at": "…Z",
  "failure_code": null, "failure_message": null,
  "data": {
    "invoice_number": { "value": "INV-1", "confidence": "0.97" },
    "invoice_date":   { "value": "15 Jan 2026", "confidence": null },
    "…": "… every scalar field, always present …",
    "line_items": [
      { "description": {"value": "Widget", "confidence": "0.9"},
        "quantity": {"value": "2", "confidence": null},
        "unit_price": {"value": "10.00", "confidence": null},
        "line_total": {"value": "20.00", "confidence": null} }
    ]
  }
}
```

- Every scalar field and both keys of every `{value, confidence}` pair are always
  present; values and confidences may be `null`. There is no document-level
  confidence.
- Money, quantity, and confidence values serialize as JSON strings (exact
  decimals, no floating-point artifacts). Confidence is in `[0, 1]` or `null`.
- Dates are raw extracted strings; currency is the upper-cased 3-letter form.
  Neither is interpreted until normalization.
- The raw provider payload is internal audit data and is **never** in a
  response. `failure_code` / `failure_message` are the only failure detail
  exposed and are client-safe.

### Processing lifecycle

`ExtractionService` (`app/services/processing/extraction/`) drives one attempt:

```text
document:  UPLOADED ─┐                     ┌─> COMPLETED
                     ├─> PROCESSING ──────┤
FAILED ──(retry)─────┘                     └─> FAILED ──(retry)─> PROCESSING ...

attempt:   PROCESSING ─> COMPLETED | FAILED     (terminal; a retry is a new attempt)
```

- `start` runs the first attempt for an `UPLOADED` document; `retry` runs a new
  attempt for one whose last extraction `FAILED`. Any other current status is a
  `409` (`EXTRACTION_IN_PROGRESS`, `DOCUMENT_ALREADY_EXTRACTED`,
  `EXTRACTION_NOT_FAILED`, `DOCUMENT_NOT_EXTRACTABLE`).
- `PROCESSING` — on both the attempt row and the document — is committed
  **before** the provider runs, so an interrupted run stays visible. A
  `SELECT ... FOR UPDATE` on the document plus a partial unique index keep at
  most one active attempt per document.
- A failed attempt writes only `FAILED` + a client-safe
  `failure_code` / `failure_message`. It never touches the stored PDF, never
  persists a partial or schema-invalid result as success, and never mutates an
  earlier attempt — history is preserved as `attempt_number` 1, 2, 3, …
- `NEEDS_REVIEW` is not used in Stage 3. `COMPLETED` means extraction finished,
  not normalization or validation.

### PDF preprocessing

`prepare_document(document, storage)` (`preprocessing.py`) turns a stored
original into provider-ready input, independent of any AI provider:

- resolves and **reads** the PDF through the storage service (never an arbitrary
  path) — it never writes;
- pulls the embedded text layer out page by page and normalizes whitespace;
- classifies the result as `DIGITAL` (every page has usable text), `ABSENT`
  (a full scan — no page reaches `MIN_USEFUL_CHARS_PER_PAGE`), or `PARTIAL`, and
  exposes `ocr_page_numbers` for the scanned pages;
- returns the untouched `pdf_bytes` so a future OCR/vision step can rasterize —
  actual page rendering is deferred with the real provider (no rasterization
  dependency yet).
- Errors: `PDF_UNAVAILABLE` (storage has no readable file), `PDF_UNREADABLE`
  (bytes are not a parseable PDF), `TOO_MANY_PAGES`.

The start and retry endpoints run this preprocessing before invoking the
provider. The provider consumes `PreparedDocument`, not a database model or file
path. Preprocessing failures create a safe `FAILED` attempt with the relevant
code; they never expose the stored path or modify the original.

### Extraction provider

`ExtractionProvider` (`provider.py`) is the whole boundary: a `name` and one
`async extract(prepared) -> payload` method. Application code depends on this,
never on a provider SDK. Failure modes are typed `ProviderError` subclasses
(`PROVIDER_TIMEOUT`, `PROVIDER_RATE_LIMITED`, `PROVIDER_UNAVAILABLE`); the
composition layer turns them (and preprocessing errors) into a client-safe
`FAILED` attempt. A *malformed* result is returned, not raised, so the service
can reject it as `MALFORMED_EXTRACTION` and keep "answered badly" distinct from
"failed".

`FakeExtractionProvider` (`fake.py`) is the offline implementation used in
development and every automated test — no external calls. `FakeBehavior`
selects `SUCCESS` (a fixed payload seeded by document id), `MALFORMED`,
`TIMEOUT`, `RATE_LIMITED`, or `ERROR`.

The provisional first real adapter is Azure AI Document Intelligence
`prebuilt-invoice`; AWS Textract `AnalyzeExpense` is the comparison candidate.
Neither has yet been benchmarked on LedgerDrop, so this is not a validated
production selection. Confidence remains `null` at application level until a
representative live evaluation demonstrates that provider scores track actual
correctness. The evidence gates, proposed mapping, and inert config keys are in
[`docs/provider-selection.md`](../docs/provider-selection.md).

### Evaluation dataset

`evaluation/` holds a small synthetic invoice set (generated from
`evaluation/expected.json`, no third-party data) with ground-truth field values,
covering digital, scanned, multi-page, incomplete, multi-line-item,
unusual-layout, low-quality, and non-invoice PDFs. `evaluation.dataset.load_cases()`
loads it; `evaluation.scoring.score_run({case_id: InvoiceExtraction})` grades a
provider's output — overall / critical-field / line-item accuracy, per category.
See `evaluation/README.md`.

## Tests

```bash
# from the repository root
docker compose up -d db

# from backend/
uv run pytest
```

The suite runs against PostgreSQL (the project uses PostgreSQL only). It connects
with `DATABASE_URL` but swaps in a dedicated `ledgerdrop_test` database, which it
creates automatically and rebuilds for every test. Set `TEST_DATABASE_URL` to
point at a different server. No services beyond PostgreSQL are required.

## Layout

```text
app/
  main.py            FastAPI entry point / app factory
  core/
    config.py        environment-based settings (pydantic-settings)
    errors.py        APIError hierarchy + exception handlers (one error envelope)
  database/
    base.py          declarative Base + naming conventions
    session.py       async engine, session factory, get_db dependency
  models/
    document.py      the documents table
    extraction.py    invoice_extractions + invoice_line_items tables
  schemas/
    document.py      DocumentRead - public metadata (no file_location / file_hash)
    extraction.py             internal invoice extraction contract ({value, confidence})
    extraction_persistence.py flat <-> nested mapping for the extraction tables
    extraction_api.py         extraction request + public response models
  services/
    pdf.py           inspect_pdf: signature + readability + page-count check
    storage/
      local.py       LocalFileStorage: atomic writes, path-traversal safe
    processing/
      extraction/
        lifecycle.py      valid document / attempt status transitions
        repository.py      ExtractionRepository - reads/writes the extraction tables
        service.py         ExtractionService - start / retry, PROCESSING -> COMPLETED|FAILED
        preprocessing.py   stored PDF -> per-page text + OCR flags (provider-independent)
        provider.py        ExtractionProvider interface + ProviderError hierarchy
        fake.py            FakeExtractionProvider - deterministic offline double
  api/
    deps.py          shared dependencies (get_db, get_storage, get_extraction_service, get_extractor)
    documents.py     POST /documents, GET /documents[/{id}[/file]]
    extractions.py   POST/GET /documents/{id}/extractions[...]
    health.py        health endpoints
    router.py        aggregate router
evaluation/          synthetic invoice set + ground truth (extraction accuracy)
alembic/             migration environment (async) + versions/
  tests/               pytest suite (PostgreSQL, no services beyond the database)
```

## Error response shape

Every error response uses one envelope:

```json
{ "error": { "code": "NOT_FOUND", "message": "…" } }
```

`details` is added (as an array) only when it carries structured information -
for example per-field validation errors on a `VALIDATION_ERROR`. Internal
exception text is never exposed; unexpected errors always collapse to a generic
`INTERNAL_ERROR` 500.
