# LedgerDrop backend

FastAPI + async SQLAlchemy service. Stage 2 built the **upload foundation** (app
skeleton, configuration, database layer, migrations, consistent API errors,
local file storage); Stage 3 added **structured invoice extraction** and Stage 4
added deterministic **normalization** of a completed extraction, wired together
as `upload → extraction → normalization`. Validation and decision/escalation are
not implemented yet.

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

Stage 3 adds structured invoice extraction. The endpoints run on the
**deterministic offline fake provider** (`FakeExtractionProvider`,
`provider_name` `fake-deterministic`) by default, or the real
**OpenAI GPT-5-mini adapter** (`OpenAIExtractionProvider`, `provider_name`
`openai`) when `EXTRACTION_PROVIDER=openai` and `OPENAI_API_KEY` are set — see
[`docs/provider-selection.md`](../docs/provider-selection.md).

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

`OpenAIExtractionProvider` (`openai_provider.py`) is the real adapter: OpenAI
GPT-5-mini, called through the Responses API with a strict `json_schema`
response format naming every `InvoiceExtraction` field, so the API itself
rejects any other shape. The original PDF bytes are sent as-is (no local
rendering); OpenAI extracts text and page images from the PDF itself. The
decoded response is mapped into the same payload shape the fake provider
returns and still goes through the same `InvoiceExtraction` validation before
persistence — nothing about a provider response is trusted without that check.
GPT-5-mini has no calibrated per-field confidence, so the adapter never asks
for one: every field's confidence is `None`, unconditionally. The full raw API
response is kept only as internal audit data
(`ExtractionAttempt.raw_response`), never returned by a public endpoint.
Configured via `EXTRACTION_PROVIDER=openai`, `OPENAI_API_KEY`, `OPENAI_MODEL`
(default `gpt-5-mini`); `EXTRACTION_PROVIDER` stays `fake` (the default) until
a real key is supplied.

This was selected without running the Azure/AWS bake-off Stage 3 originally
called for; Azure AI Document Intelligence `prebuilt-invoice` remains a
candidate for a later migration behind the same `ExtractionProvider`
interface. Full rationale in
[`docs/provider-selection.md`](../docs/provider-selection.md).

### Evaluation dataset

`evaluation/` holds a small synthetic invoice set (generated from
`evaluation/expected.json`, no third-party data) with ground-truth field values,
covering digital, scanned, multi-page, incomplete, multi-line-item,
unusual-layout, low-quality, and non-invoice PDFs. `evaluation.dataset.load_cases()`
loads it; `evaluation.scoring.score_run({case_id: InvoiceExtraction})` grades a
provider's output — overall / critical-field / line-item accuracy, per category.
See `evaluation/README.md`.

## Normalization API (Stage 4)

Stage 4 turns the raw, immutable output of a **completed** extraction into
separate canonical values, recording a structured field error wherever a value
is invalid or ambiguous. It is fully deterministic and offline — no AI, no
network, no provider to configure. Full spec in
[`../docs/stage-4-normalization.md`](../docs/stage-4-normalization.md).

Every path hangs off a Stage 3 extraction attempt:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/documents/{document_id}/extractions/{extraction_id}/normalizations` | start the first normalization (`201`) |
| `POST` | `/documents/{document_id}/extractions/{extraction_id}/normalizations/retry` | run a new attempt after a technical failure (`201`) |
| `GET`  | `/documents/{document_id}/extractions/{extraction_id}/normalizations` | every attempt, newest first |
| `GET`  | `/documents/{document_id}/extractions/{extraction_id}/normalizations/latest` | the most recent attempt |
| `GET`  | `/documents/{document_id}/extractions/{extraction_id}/normalizations/{normalization_id}` | one specific attempt |

- `404 DOCUMENT_NOT_FOUND` / `EXTRACTION_NOT_FOUND` for an unknown or
  wrong-document id; `404 NORMALIZATION_NOT_FOUND` when the extraction has no
  matching attempt.
- `409` when the extraction cannot legally transition:
  `EXTRACTION_NOT_COMPLETED` (source extraction has not completed),
  `NORMALIZATION_IN_PROGRESS`, `EXTRACTION_ALREADY_NORMALIZED` (a second
  `start`), `NORMALIZATION_FAILED` (a `start` after a technical failure — use
  `retry`), `NORMALIZATION_NOT_FAILED` (`retry` when the last attempt did not
  fail technically).
- A normalization that *runs* and hits a technical failure is still `201` — the
  attempt was created; its `status` is `FAILED` and `failure_code` /
  `failure_message` (both client-safe) say why. Use `retry` to try again.
- A **field-level** normalization error is not a technical failure: the attempt
  is `COMPLETED` and the error travels inside `data.errors` with a stable
  `field_path`, the stringified `raw_value`, a safe `code`
  (`invalid_date`, `invalid_currency`, `unknown_currency`, `invalid_number`,
  `ambiguous_number`, `text_too_long`), and a safe `message`.

**Request** (`NormalizationStartRequest`) — start and retry take an empty JSON
body (`{}` or none). There are no parameters; unknown keys are rejected with
`422 VALIDATION_ERROR`.

**Response** (`InvoiceNormalizationResult`):

```jsonc
{
  "normalization_id": "…", "extraction_id": "…", "attempt_number": 1,
  "status": "PROCESSING | COMPLETED | FAILED",
  "started_at": "…Z", "completed_at": "…Z" | null,
  "created_at": "…Z", "updated_at": "…Z",
  "failure_code": null, "failure_message": null,
  "data": {
    "invoice_number": "INV-1",
    "invoice_date": "2026-01-15",        // canonical YYYY-MM-DD, or null
    "currency": "EUR",                    // approved ISO 4217 code, or null
    "total_amount": "119.00",            // decimal as a JSON string
    "…": "… every canonical scalar, always present, single value or null …",
    "line_items": [
      { "description": "Widget", "quantity": "2",
        "unit_price": "10.00", "line_total": "20.00" }
    ],
    "errors": [
      { "field_path": "due_date", "raw_value": "15/13/2026",
        "code": "invalid_date", "message": "…client-safe…" }
    ]
  }
}
```

- Normalized fields hold a **single canonical value or `null`** and carry **no
  confidence** (it stays on the Stage 3 record). There is no `document_id` and
  no raw provider payload in the response.
- Money and quantity values serialize as JSON strings (exact decimals, sign and
  scale preserved, never rounded).
- The normalized record references its source `extraction_id` and never mutates
  it or the original PDF. Schema changes go through Alembic
  (`0003_normalization_tables`).

### Normalization lifecycle

`NormalizationService` (`app/services/processing/normalization/`) drives one
attempt:

```text
COMPLETED extraction ─> PROCESSING ─> COMPLETED | FAILED
FAILED normalization ─(retry)─> PROCESSING ─> COMPLETED | FAILED
```

- `PROCESSING` is committed **before** the deterministic engine runs. A
  `SELECT ... FOR UPDATE` on the source extraction row plus a partial unique
  index keep at most one active attempt per extraction; a lost race becomes a
  `409`.
- Only an infrastructure problem (source extraction unreadable, a database write
  failure, an unexpected engine exception) makes an attempt `FAILED`, and it
  rolls back with no partial result and a generic `NORMALIZATION_FAILED` reason.
  History is preserved as `attempt_number` 1, 2, 3, …
- Extraction and normalization stay independently callable; normalization never
  changes the document or extraction rows.

## Validation API (Stage 5)

Stage 5 evaluates a closed catalogue of deterministic rules against a
**completed** normalization attempt and records structured findings. It
reports facts only — a rule violation completes the attempt with findings, not
a technical failure — and never decides acceptance, rejection, or escalation
(no `NEEDS_REVIEW` anywhere; that is a later decision stage). It is fully
deterministic and offline — no AI, no network, no provider to configure. Full
spec in [`../docs/stage-5-validation.md`](../docs/stage-5-validation.md).

Every path hangs off a Stage 4 normalization attempt:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/documents/{document_id}/extractions/{extraction_id}/normalizations/{normalization_id}/validations` | start the first validation (`201`) |
| `POST` | `/documents/{document_id}/extractions/{extraction_id}/normalizations/{normalization_id}/validations/retry` | run a new attempt after a technical failure (`201`) |
| `GET`  | `/documents/{document_id}/extractions/{extraction_id}/normalizations/{normalization_id}/validations` | every attempt, newest first |
| `GET`  | `/documents/{document_id}/extractions/{extraction_id}/normalizations/{normalization_id}/validations/latest` | the most recent attempt |
| `GET`  | `/documents/{document_id}/extractions/{extraction_id}/normalizations/{normalization_id}/validations/{validation_id}` | one specific attempt |

- `404 DOCUMENT_NOT_FOUND` / `EXTRACTION_NOT_FOUND` / `NORMALIZATION_NOT_FOUND`
  for an unknown or wrong-chain id; `404 VALIDATION_NOT_FOUND` when the
  normalization has no matching attempt.
- `409` when the normalization cannot legally transition:
  `NORMALIZATION_NOT_COMPLETED` (source normalization has not completed),
  `VALIDATION_IN_PROGRESS`, `NORMALIZATION_ALREADY_VALIDATED` (a second
  `start`), `VALIDATION_FAILED` (a `start` after a technical failure — use
  `retry`), `VALIDATION_NOT_FAILED` (`retry` when the last attempt did not
  fail technically).
- A validation that *runs* and hits a technical failure is still `201` — the
  attempt was created; its `status` is `FAILED` and `failure_code` /
  `failure_message` (both client-safe) say why. Use `retry` to try again.
- A **rule violation** is not a technical failure: the attempt is `COMPLETED`
  and the finding travels inside `data.findings` with a closed `rule` code, a
  `severity` of `error | warning | info`, an optional Stage 4 `field_path`,
  client-safe `expected` / `actual` values, a fixed `message`, and a `context`
  object.

**Request** (`ValidationStartRequest`) — start and retry take an empty JSON
body (`{}` or none). There are no parameters; unknown keys are rejected with
`422 VALIDATION_ERROR`.

**Response** (`InvoiceValidationResult`):

```jsonc
{
  "validation_id": "…", "normalization_id": "…", "attempt_number": 1,
  "status": "PROCESSING | COMPLETED | FAILED",
  "started_at": "…Z", "completed_at": "…Z" | null,
  "created_at": "…Z", "updated_at": "…Z",
  "failure_code": null, "failure_message": null,
  "data": {
    "findings": [
      { "rule": "totals_do_not_reconcile", "severity": "warning",
        "field_path": "total_amount", "expected": "119.00", "actual": "120.00",
        "message": "…client-safe…", "context": { "tolerance": "0.01" } }
    ],
    "summary": { "total": 1, "error": 0, "warning": 1, "info": 0 }
  }
}
```

- `summary` is re-derived from `findings` on every read, never stored, so it
  cannot drift. There is no `document_id`, no confidence, and no
  acceptance/rejection/escalation field anywhere in the response.
- The validation record references its source `normalization_id` and never
  mutates it or any Stage 2–4 row. Schema changes go through Alembic
  (`0004_validation_tables`).

### Validation lifecycle

`ValidationService` (`app/services/processing/validation/`) drives one
attempt, mirroring Stage 4 exactly:

```text
COMPLETED normalization ─> PROCESSING ─> COMPLETED | FAILED
FAILED validation        ─(retry)─> PROCESSING ─> COMPLETED | FAILED
```

- `PROCESSING` is committed **before** the deterministic rule engine runs. A
  `SELECT ... FOR UPDATE` on the source normalization row plus a partial
  unique index keep at most one active attempt per normalization; a lost race
  becomes a `409`.
- Only an infrastructure problem (source normalization unreadable, a database
  write failure, an unexpected engine exception) makes an attempt `FAILED`,
  and it rolls back with no partial result and a generic `VALIDATION_FAILED`
  reason. History is preserved as `attempt_number` 1, 2, 3, …
- Normalization and validation stay independently callable; validation never
  changes the document, extraction, or normalization rows.

### Processing pipeline

`ProcessingPipeline` (`app/services/processing/pipeline.py`) composes the stages
into `upload → extraction → normalization → validation`. It adds no processing
rules — it runs the extraction stage, then, only when it ended `COMPLETED`, the
normalization stage, then, only when *that* ended `COMPLETED`, the validation
stage, against one session.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/documents/{document_id}/pipeline` | run extraction, then normalization, then validation (`201`) |
| `POST` | `/documents/{document_id}/pipeline/retry` | retry a failed extraction, then normalize and validate the new attempt (`201`) |

**Response** (`PipelineRunResult`) — the three per-stage public results side by
side, no fields of its own:

```jsonc
{
  "extraction":    { /* InvoiceExtractionResult */ },
  "normalization": { /* InvoiceNormalizationResult */ } | null,
  "validation":    { /* InvoiceValidationResult */ } | null
}
```

- `normalization` is `null` when the extraction did not complete (nothing to
  normalize) — retry the pipeline. When present it may itself be `FAILED` (a
  normalization technical failure that left the completed extraction intact) or
  `COMPLETED` with field-level errors in `normalization.data.errors`.
- `validation` is `null` when `normalization` is absent or did not complete
  (nothing to validate). When present it may itself be `FAILED` (a validation
  technical failure that left the completed normalization intact) or
  `COMPLETED` with findings in `validation.data.findings` — a finding is not a
  failure and never implies acceptance, rejection, or `NEEDS_REVIEW`.
- A run that *starts* is `201` even if a later stage then fails; the stage's
  `status` and `failure_code` say what happened. The extraction stage's own
  `404` (`DOCUMENT_NOT_FOUND`) and `409` (`DOCUMENT_ALREADY_EXTRACTED`,
  `EXTRACTION_NOT_FAILED`, …) propagate unchanged. Unknown body keys →
  `422 VALIDATION_ERROR`.
- The per-stage endpoints (`/extractions[...]`,
  `/extractions/{eid}/normalizations[...]`, and
  `/extractions/{eid}/normalizations/{nid}/validations[...]`) are unchanged and
  remain the way to drive or inspect one stage in isolation.

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
point at a different server. No services beyond PostgreSQL are required, and no
test calls an external AI service — extraction runs on the deterministic fake
provider, and normalization and validation make no network call at all.

`tests/test_stage4_verification.py` is the Stage 4 acceptance checklist: every
bullet (date formats, ambiguous/impossible dates, currency validity, separator
rules, negative/malformed amounts, quantity parsing, text/identifier
preservation, null/empty values, line items, structured errors, DB constraints,
lifecycle/retry, concurrency, transaction rollback, retrieval + `404`/`409`,
Stage 3 preservation, no-AI/no-network) maps to an assertion there, mostly
exercised end to end through the engine + service + API + pipeline.

`tests/test_stage5_verification.py` is the Stage 5 acceptance checklist the same
way. Its docstring maps every bullet (every rule passing/failing at its
tolerance edges, missing-required-field + normalization-error co-occurrence,
date/reconciliation/confidence/duplicate/high-value behaviour, lifecycle,
concurrency, transactional rollback, retrieval + `404`/`409`, DB relationships,
no-AI/no-network) to the dense per-layer test file (`test_validation_rules.py`,
`test_validation_engine.py`, `test_validation_service.py`,
`test_validations_api.py`, …) that already covers it exhaustively, and adds
only what had no automated coverage yet: golden clean / many-finding /
duplicate invoices run end to end through the real API and pipeline, an
API-level concurrent-start race, a full-chain byte-for-byte immutability check
(every Stage 2–4 row and the stored PDF bytes, before vs. after a validation
call), and an `alembic upgrade head → downgrade -1 → upgrade head → check`
round trip against a dedicated throwaway database proving Stage 2–4 data
survives byte-for-byte.

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
    normalization.py invoice_normalizations + normalized line items + field errors
    validation.py    invoice_validations + invoice_validation_findings tables
  schemas/
    document.py                  DocumentRead - public metadata (no file_location / file_hash)
    extraction.py                internal invoice extraction contract ({value, confidence})
    extraction_persistence.py    flat <-> nested mapping for the extraction tables
    extraction_api.py            extraction request + public response models
    normalization.py             internal normalized invoice contract (single value | null, no confidence)
    normalization_persistence.py nested <-> flat mapping for the normalization tables
    normalization_api.py         normalization request + public response models
    validation.py                internal validation contract (findings + re-derived summary, no decision vocabulary)
    validation_catalogue.py      closed RuleSpec catalogue - one entry per ValidationRule
    validation_persistence.py    InvoiceValidation <-> flat finding-row mapping
    validation_api.py            validation request + public response models
    pipeline_api.py              PipelineRunRequest + PipelineRunResult (all three stage results)
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
        openai_provider.py OpenAIExtractionProvider - real adapter (GPT-5-mini)
      normalization/
        normalizers.py    deterministic field normalizers (date / currency / number / text)
        iso4217.py        vendored approved-currency allow-list (no network)
        engine.py         normalize_extraction - contract -> canonical, collects field errors
        lifecycle.py      valid extraction / normalization-attempt transitions
        repository.py      NormalizationRepository - reads/writes the normalization tables
        service.py         NormalizationService - start / retry, PROCESSING -> COMPLETED|FAILED
      validation/
        policy.py         every provisional ⚠ Part 2 constant (Decimal), vendored - nowhere else
        rules.py          one pure check_<rule>(RuleContext) per catalogue member + run_rules
        engine.py          read-only evaluate(session, normalization_id, *, started_at) -> InvoiceValidation
        lifecycle.py       valid normalization / validation-attempt transitions
        repository.py      ValidationRepository - reads/writes the validation tables
        service.py         ValidationService - start / retry, PROCESSING -> COMPLETED|FAILED
      pipeline.py       ProcessingPipeline - composes extraction -> normalization -> validation (no rules of its own)
  api/
    deps.py          shared dependencies (get_db, get_storage, get_extraction_service, get_extractor, get_normalization_service, get_validation_service, get_pipeline)
    documents.py     POST /documents, GET /documents[/{id}[/file]]
    extractions.py   POST/GET /documents/{id}/extractions[...]
    normalizations.py POST/GET /documents/{id}/extractions/{eid}/normalizations[...]
    validations.py   POST/GET /documents/{id}/extractions/{eid}/normalizations/{nid}/validations[...]
    pipeline.py      POST /documents/{id}/pipeline[/retry]
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
