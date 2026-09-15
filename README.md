# LedgerDrop

Business document-processing application. Users upload business documents; the
system extracts, normalizes, and validates structured data, then accepts the
result or routes it for human review.

**Project status: closed after Stage 9.** The invoice MVP and Stage 9
deployment-readiness implementation are complete. No Stage 10 is planned.
Container images, `render.yaml` for Render, a Cloudflare R2 storage backend
behind `FileStorage`, production config with a fail-fast safety check, edge
hardening, structured logging + `/metrics` + readiness checks, and an operator
runbook (`docs/stage-9-deployment-readiness.md`, `docs/stage-9-runbook.md`).
Live Render + R2 operational acceptance is not evidenced in this repository;
the release, rollback, backup-restore, and alert rehearsals remain governed by
the [Stage 9 acceptance gate](docs/stage-9-deployment-readiness.md). See the
[project handoff](docs/project-handoff.md) before resuming development or
operating a production deployment.

**Stage 8 (reviewer corrections) is complete.** Upload,
structured invoice extraction, deterministic normalization, deterministic
validation, the deterministic decision, and human review (Stages 2–7) are
complete for English-language PDF invoices. Stage 6 turns a completed
validation into an `ACCEPTED` / `NEEDS_REVIEW` decision with ordered reasons
and moves a document to `NEEDS_REVIEW` on that outcome. Stage 7 records the
human resolution of a `NEEDS_REVIEW` decision — `APPROVE` / `REJECT` with an
audit trail — moving the document to a terminal `APPROVED` / `REJECTED` status
(`/reviews/queue`, `.../decisions/{id}/review`, plus a reviewer UI at `/review`
in `frontend/`). Stage 8 lets a reviewer **correct** a document's canonical
invoice data in an append-only store (`/documents/{id}/corrections`), re-runs
normalization (as a `base ⊕ corrections` merge projection), validation, and the
decision against the corrected data, and resolves the document status —
auto-accepting a clean re-decision or returning it to the review queue. See
[CLAUDE.md](CLAUDE.md) for the full scope.

## Repository layout

| Path | Contents |
|------|----------|
| [backend/](backend/) | FastAPI + async SQLAlchemy service. See [backend/README.md](backend/README.md). |
| [frontend/](frontend/) | Next.js + TypeScript interface. See [frontend/README.md](frontend/README.md). |
| `storage/uploads/` | Local development file storage for uploaded PDFs. |
| [docker-compose.yml](docker-compose.yml) | Local PostgreSQL for development and tests. |
| [docker-compose.prod.yml](docker-compose.prod.yml) | Local production-like stack (prod images + MinIO). Verification only. |
| [render.yaml](render.yaml) | Render Blueprint: staging + production topology (Stage 9). |
| [docs/stage-9-deployment-readiness.md](docs/stage-9-deployment-readiness.md), [docs/stage-9-runbook.md](docs/stage-9-runbook.md) | Deployment spec and operator runbook. |

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

## Production-like run (offline)

```bash
docker compose -f docker-compose.prod.yml up --build
```

Builds the real backend/frontend images and runs them with PostgreSQL and a
MinIO S3 backend. For real deployment see
[docs/stage-9-runbook.md](docs/stage-9-runbook.md).

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
