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
  schemas/
    document.py      DocumentRead - public metadata (no file_location / file_hash)
  services/
    pdf.py           inspect_pdf: signature + readability + page-count check
    storage/
      local.py       LocalFileStorage: atomic writes, path-traversal safe
  api/
    deps.py          shared dependencies (get_db, get_storage)
    documents.py     POST /documents, GET /documents[/{id}[/file]]
    health.py        health endpoints
    router.py        aggregate router
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
