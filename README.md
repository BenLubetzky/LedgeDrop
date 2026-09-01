# LedgerDrop

Business document-processing application. Users upload business documents; the
system extracts, normalizes, and validates structured data, then accepts the
result or routes it for human review.

**Current stage: Stage 2 - the upload foundation.** The first use case is
English-language PDF invoices. Invoice extraction and later processing stages are
not implemented yet. See [CLAUDE.md](CLAUDE.md) for the full scope and
[docs/processing-spec.md](docs/processing-spec.md) for the deferred processing design.

## Repository layout

| Path | Contents |
|------|----------|
| [backend/](backend/) | FastAPI + async SQLAlchemy service. See [backend/README.md](backend/README.md). |
| `frontend/` | Next.js + TypeScript UI (not yet implemented). |
| `storage/uploads/` | Local development file storage for uploaded PDFs. |
| [docker-compose.yml](docker-compose.yml) | Local PostgreSQL for development and tests. |

## Local development

```bash
# 1. Start PostgreSQL
docker compose up -d db

# 2. Backend
cd backend
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload      # http://localhost:8000  (docs at /docs)

# 3. Tests
uv run pytest
```

Requirements: [uv](https://docs.astral.sh/uv/), Docker + Docker Compose.
