# LedgerDrop

Business document-processing application. Users upload business documents; the
system extracts, normalizes, and validates structured data, then accepts the
result or routes it for human review.

**Current stage: Stage 7 (human review) is complete.** Upload, structured
invoice extraction, deterministic normalization, deterministic validation, the
deterministic decision, and human review (Stages 2–7) are complete for
English-language PDF invoices. Stage 6 turns a completed validation into an
`ACCEPTED` / `NEEDS_REVIEW` decision with ordered reasons and moves a document
to `NEEDS_REVIEW` on that outcome. Stage 7 records the human resolution of a
`NEEDS_REVIEW` decision — `APPROVE` / `REJECT` with an audit trail — moving the
document to a terminal `APPROVED` / `REJECTED` status, with a backend API
(`/reviews/queue`, `.../decisions/{id}/review`) and a reviewer UI (`/review` in
`frontend/`). Field corrections and reprocessing are deferred to Stage 8. See
[CLAUDE.md](CLAUDE.md) for the full scope.

## Repository layout

| Path | Contents |
|------|----------|
| [backend/](backend/) | FastAPI + async SQLAlchemy service. See [backend/README.md](backend/README.md). |
| [frontend/](frontend/) | Next.js + TypeScript interface. See [frontend/README.md](frontend/README.md). |
| `storage/uploads/` | Local development file storage for uploaded PDFs. |
| [docker-compose.yml](docker-compose.yml) | Local PostgreSQL for development and tests. |

## Requirements

- [uv](https://docs.astral.sh/uv/) — Python toolchain and backend dependencies
- Node.js 20+ and npm — frontend dependencies
- Docker + Docker Compose — local PostgreSQL

## Local development

```bash
# 1. Start PostgreSQL
docker compose up -d db

# 2. Backend  (http://localhost:8000, API docs at /docs)
cd backend
cp .env.example .env
uv sync
uv run alembic upgrade head          # apply database migrations
uv run uvicorn app.main:app --reload

# 3. Frontend  (http://localhost:3000)
cd frontend
cp .env.example .env.local
npm install
npm run dev
```

Run each service in its own terminal. The frontend talks to the backend at
`NEXT_PUBLIC_API_BASE_URL` (default `http://localhost:8000`).

## Tests

```bash
cd backend
uv run pytest          # needs the PostgreSQL container running
```

## Database migrations

```bash
cd backend
uv run alembic upgrade head                                   # apply all
uv run alembic downgrade -1                                   # roll back one
uv run alembic revision --autogenerate -m "describe change"   # create a new one
```

More detail in [backend/README.md](backend/README.md).
