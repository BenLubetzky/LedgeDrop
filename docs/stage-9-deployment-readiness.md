# Stage 9: deployment readiness and MVP hardening — specification

`CLAUDE.md` carries the high-level summary and the ordered work packages; this
file holds the deployment boundary, the pinned platform baseline, the concrete
per-package design, the completion gate, and the verification map.

**Status.** All in-repo work for Packages 1–9 is done; what remains is
**operator execution against live infrastructure** (a Render account + R2), which
this repo cannot perform.

| Pkg | In-repo deliverable | State |
|---|---|---|
| 1 | Spec + pinned baseline + `render.yaml` | done |
| 2 | Production settings model, `DATABASE_URL` coercion, `check_deployment_safety`, `.env.production.example` | done |
| 3 | `backend/Dockerfile`, `frontend/Dockerfile`, `.dockerignore` × 2, `next.config.ts` standalone, `docker-compose.prod.yml` | done |
| 4 | `FileStorage` seam, `S3FileStorage`, `get_bytes` read path, `build_storage`, pool bounds, `migrate_uploads_to_s3.py`, moto tests | done; managed-DB/R2 provisioning + cutover = operator |
| 5 | request-id / security-headers / body-cap middleware, `TrustedHostMiddleware`, CORS narrowing, `slowapi` rate limiting, `/docs` off in prod | done |
| 6 | `/health` vs `/health/ready` (+ storage probe), JSON logging + `X-Request-ID`, Sentry hook, `/metrics` + `uploads_total` | done (per-stage pipeline metrics + queue-depth gauge thin; alert rules = operator) |
| 7 | backup/retention/restore + failed-job recovery procedures | written in runbook; tested restore + drill = operator |
| 8 | `staging_smoke.py`, corpus + regression gates | harness done; staging run = operator |
| 9 | `docs/stage-9-runbook.md` | done; release + rollback rehearsals = operator |

`render.yaml` can be applied once the operator has a Render account and the R2
buckets/tokens (runbook §1).

**Stage 9 adds no document-processing feature.** It takes the completed
Stage 2–8 product to a repeatable, supportable MVP deployment. It changes
runtime packaging, configuration, storage backends, edge security, and
operations — it does **not** change extraction, normalization, validation,
decision, review, or correction contracts, and it does not add authentication.

**Policy calls are business-judgement calls, not derived facts.** The four
opened at the start of Package 1 have been pinned by the project owner:

1. **Target platform: Render.** Managed PostgreSQL, persistent Docker web
   services, per-environment non-secret configuration groups, service-scoped
   prompted secrets, and Blueprint (`render.yaml`)
   infrastructure-as-code. Chosen over Fly.io / a single VPS / AWS for the
   lowest operational surface on a modular-monolith MVP.
2. **Durable object storage: Cloudflare R2.** S3-compatible, no egress fees,
   independent of the compute platform. A new `S3FileStorage` backend
   implements the storage interface; `LocalFileStorage` stays the development
   and test default.
3. **Environments: staging and production.** Each is a separate set of Render
   services with its own database and its own R2 bucket. Staging exists so the
   Package 8 end-to-end verification and the Package 9 release / rollback
   rehearsals run against production-like infrastructure before production is
   touched.
4. **This is still the invoice MVP.** Authentication, organizations,
   multi-tenancy, notifications, reviewer assignment, downstream posting,
   line-item re-ordering, and non-PDF input remain out of scope. Operational
   security controls required to deploy safely are in scope; building an
   identity or permissions product is not.

Values that remain the project owner's judgement and are **provisional
defaults pending review** are marked ⚠ and collected in
[Appendix A](#appendix-a--provisional--values-register). Changing a pinned
value later is a doc edit plus the matching code / infrastructure edit, not a
design change.

---

## Part 1 — Deployment boundary

### 1.1 What Stage 9 is

Stage 9 is the work that lets **one reviewed git revision** be deployed
repeatably to staging and then production, kept running, observed, and
recovered. Its inputs are the current codebase and the pinned baseline in
Part 2. Its outputs are:

- infrastructure-as-code (`render.yaml`), production container images, and
  production configuration templates checked into the repository;
- a production `S3FileStorage` backend behind the existing storage seam;
- edge and application hardening (HTTPS, trusted hosts, CORS, security headers,
  body limits, rate limiting) with server-authoritative PDF validation intact;
- liveness / readiness checks, structured request-correlated logs, metrics,
  error monitoring, and alerts that do not record invoice contents;
- backup / retention / restore procedures with one tested restoration;
- `docs/stage-9-runbook.md` and one rehearsed release plus one rehearsed
  rollback.

### 1.2 What Stage 9 must NOT do

- **Change any Stage 2–8 API contract, schema, or lifecycle.** Endpoints,
  request / response shapes, `documents.status` values, and attempt-chain
  semantics stay exactly as Stages 2–8 left them. New migrations are additive
  only and are not required by this stage.
- **Mutate stored data.** Existing `documents` rows, every processing /
  review / correction row, and every stored PDF must remain readable and
  byte-for-byte unchanged after the storage and database cutover.
- **Add authentication, roles, organisations, or tenancy.** The reviewer name
  stays an explicitly unverified label.
- **Add a new document type, provider, or processing behaviour.** The
  extraction provider selection is unchanged; production simply pins
  `EXTRACTION_PROVIDER=openai`.
- **Introduce provider-specific infrastructure before the baseline is
  recorded.** Part 2 is the gate for Packages 2–9.
- **Broaden input support.** PDF-only, English invoices, one file per request,
  ≤ 20 MB, ≤ 10 pages — enforced server-side — is preserved end to end.

### 1.3 Position in the system

```
Browser
  |  HTTPS (Render edge, managed TLS)
  v
ledgerdrop-frontend   (Render Web Service, Docker, Next.js standalone)
  |  HTTPS, server-to-server, NEXT_PUBLIC_API_BASE_URL
  v
ledgerdrop-backend    (Render Web Service, Docker, uvicorn)
  |-- Render PostgreSQL (managed)                 <- metadata + all processing records
  |-- Cloudflare R2 bucket (S3 API)              <- original PDFs (S3FileStorage)
  `-- OpenAI API                                  <- extraction provider (production)
```

Development is unchanged: `docker compose up -d db`, `LocalFileStorage` under
`storage/uploads/`, `EXTRACTION_PROVIDER=fake`, backend and frontend run on the
host.

---

## Part 2 — Pinned deployment baseline (Package 1)

### 2.1 Platform and topology

| Concern | Decision |
|---|---|
| Compute platform | Render |
| Infra as code | `render.yaml` Blueprint at the repo root, reviewed like code |
| Backend | Render **Web Service**, environment = Docker, `backend/Dockerfile`, health check path `/health`, autodeploy **off** (deploys are explicit) |
| Frontend | Render **Web Service**, environment = Docker, `frontend/Dockerfile`, Next.js `output: "standalone"`, health check path `/` |
| Database | Render **PostgreSQL** (managed); one instance per environment |
| Object storage | Cloudflare R2 bucket per environment, reached over the S3 API |
| TLS / HTTPS | Terminated at the Render edge with managed certificates; HTTP is redirected to HTTPS |
| Migrations | Backend service **Pre-Deploy Command** `alembic upgrade head` (see Part 5.4) |
| Instance size | ⚠ Render *Starter* for both web services and the database in staging; ⚠ *Standard* in production |
| Instance count | ⚠ 1 backend, 1 frontend per environment for the MVP (no horizontal scaling assumed; see Part 5.1) |

### 2.2 Environments

Two environments, fully isolated, named by suffix:

| Resource | Staging | Production |
|---|---|---|
| Backend service | `ledgerdrop-backend-staging` | `ledgerdrop-backend` |
| Frontend service | `ledgerdrop-frontend-staging` | `ledgerdrop-frontend` |
| Database | `ledgerdrop-db-staging` | `ledgerdrop-db` |
| R2 bucket | `ledgerdrop-uploads-staging` | `ledgerdrop-uploads` |
| Domain | ⚠ `staging.<domain>` (or the Render-assigned `onrender.com` host) | ⚠ `<domain>` |
| Extraction provider | `openai` with a staging-scoped key, or `fake` for cost-free smoke runs | `openai` |

No environment shares a database, a bucket, or an API key with another. A
staging deploy never runs against production data.

### 2.3 Region, traffic, availability, cost, ownership

| Attribute | Value |
|---|---|
| Primary region | ⚠ Render **Oregon** (US West) with the R2 bucket in an aligned jurisdiction; confirm against the data-residency requirement before first production deploy |
| Data region / residency | ⚠ United States; no contractual residency commitment recorded yet |
| Expected traffic | ⚠ low — order of tens of uploads per day, single-digit concurrent reviewers |
| Availability target | ⚠ best-effort business-hours; no formal SLA for the MVP |
| Cost ceiling | ⚠ to be set by the owner before production provisioning |
| Ownership | ⚠ the project owner is the sole operator and on-call for the MVP |

### 2.4 Release acceptance checklist

A revision is releasable to an environment only when **all** hold:

1. `main` is green: full backend `pytest` suite passes, `npm run lint` is
   clean, and the frontend production build compiles
   (`npx next build --webpack` locally; Docker build in CI on Linux).
2. `alembic upgrade head` applies cleanly on a clone of that environment's
   current database, and `alembic downgrade` of the same span is defined and
   was exercised at least once (Part 5.4).
3. The production image builds reproducibly and contains no secret material
   (`.dockerignore` excludes `.env*`; build args carry no credentials).
4. Startup configuration validation (Part 3.2) passes with that environment's
   real configuration.
5. Post-deploy smoke checks (Part 10.3) pass: `/health`, `/health/ready`, an
   upload → pipeline → terminal-outcome round trip on a sample invoice, and a
   PDF re-download.
6. `docs/stage-9-runbook.md` reflects any change to deploy, migration, or
   rollback steps introduced by the revision.

### 2.5 Rollback conditions

Roll back (Part 10.4) when, after a deploy:

- `/health/ready` stays failing past the readiness grace period, or
- the upload → terminal-outcome smoke path fails, or
- the error rate or p95 pipeline latency breaches its alert threshold
  (Part 7.4) and does not recover within ⚠ 15 minutes, or
- a data-integrity problem is observed (unreadable existing document, storage
  key mismatch, migration applied against the wrong database).

Rollback restores the previous image first. Release migrations must use an
expand/contract shape that remains readable by the previous image, so a
production schema downgrade is **not automatic**. Apply a tested downgrade
only when the runbook classifies it as non-destructive; otherwise retain the
expanded schema and use a forward fix or restore the pre-migration backup
(Part 5.4).

---

## Part 3 — Production configuration and secrets (Package 2)

### 3.1 Settings model changes (`backend/app/core/config.py`) — done

- `environment` is `Literal["development", "test", "staging", "production"]`
  (default `development`); an unknown value is rejected at construction.
- `storage_backend: Literal["local", "s3"]` (default `local`).
- R2 / S3 settings, all optional at the type level and required by the
  Part 3.2 check when `storage_backend == "s3"`:
  `s3_bucket: str | None`, `s3_endpoint_url: str | None` (the R2 S3 endpoint),
  `s3_region: str = "auto"`, `s3_access_key_id: SecretStr | None`,
  `s3_secret_access_key: SecretStr | None`,
  `s3_key_prefix: str = ""` (optional object-key namespace within the bucket).
- `trusted_hosts: list[str]` (comma-split, sharing the `cors_allow_origins`
  splitter; default `["*"]` for development, must be explicit when deployed).
  The `TrustedHostMiddleware` that consumes it is wired in Package 5.
- Rate-limit knobs (Part 6.5): `rate_limit_enabled: bool = False`,
  `rate_limit_default: str = "60/minute"` ⚠,
  `rate_limit_upload: str = "10/minute"` ⚠.
- `log_format: Literal["text", "json"] = "text"` (deployed uses `json`;
  the structured-logging wiring is Package 6) and
  `sentry_dsn: SecretStr | None = None` (Package 7).
- **`database_url` scheme coercion.** A field validator rewrites a driver-less
  `postgresql://` / legacy `postgres://` URL (what Render's managed database
  injects) to `postgresql+asyncpg://`; a URL that already names a driver is
  left untouched. This is what lets `render.yaml`'s `fromDatabase` wiring feed
  both the app engine and `alembic/env.py` (both async) without a second
  secret. The localhost default is exported as `LOCAL_DATABASE_URL`.
- `get_settings()` stays `@lru_cache`; **no** usable production default for any
  secret, connection string, bucket, or origin.

### 3.2 Startup validation — fail fast — done

`check_deployment_safety(settings)` in `config.py`, called by `get_settings()`
immediately after `Settings()` is constructed, aborts process start when
`environment in {"staging", "production"}` and any of these hold:

- `debug` is true;
- `database_url` is the bundled localhost default, names a loopback host, or
  does not use PostgreSQL with the asyncpg driver;
- `cors_allow_origins` is empty, contains `*`, or contains a `localhost` /
  `127.0.0.1` / `0.0.0.0` / `::1` origin;
- `trusted_hosts` is empty or contains any wildcard;
- `storage_backend != "s3"`, or an `s3_*` value it needs is missing;
- `extraction_provider != "openai"`, or `openai_api_key` is absent;
- `log_format != "json"`.

Every failing check is collected and reported together. It is a **post-model
check raising `DeploymentConfigError` (a `RuntimeError`)**, deliberately *not* a
pydantic `model_validator`: a validator error echoes the offending input, which
for settings loaded from the environment would put secret values into the
traceback. The standalone check's message names only the settings, never a
value. Covered by `backend/tests/test_config.py` with a synthetic deployed
configuration (valid case passes; each violation raises; the message leaks no
secret; multiple problems are reported at once).

### 3.3 Secrets handling — done

- Non-secret shared configuration lives in Render **Environment Groups**
  (`ledgerdrop-staging`, `ledgerdrop-production`). Render does not permit
  `sync: false` entries in environment groups, so prompted secrets are declared
  directly on the matching backend service, plus managed in the R2 dashboard.
  Nothing secret is committed.
- The root `.gitignore` and `frontend/.gitignore` now also re-include
  `!.env.production.example`. `backend/.env.production.example` and
  `frontend/.env.production.example` are **documentation only** — placeholder
  values, each secret marked `SECRET` with its source (Render group /
  `fromDatabase`, R2 token, OpenAI dashboard, Sentry). They are never copied to
  a server. `backend/.env.example` gained the new development-facing knobs
  (`LOG_FORMAT`, `TRUSTED_HOSTS`, `STORAGE_BACKEND`, rate-limit) at their dev
  defaults.
- `render.yaml` references service env vars by key with `sync: false` for every secret
  (Render prompts for the value at Blueprint apply time and never stores it in
  the file).
- Secret rotation procedure is documented in the runbook (Part 10.7):
  update the Render group / R2 token, redeploy, verify `/health/ready`, revoke
  the old credential.

### 3.4 Proxy and origin behaviour

- The backend runs behind the Render edge proxy. Configure uvicorn with
  `--proxy-headers` and `--forwarded-allow-ips="*"` (the edge is the only
  ingress) so client scheme / host are read from `X-Forwarded-*`.
- `TrustedHostMiddleware` uses `trusted_hosts`; `CORSMiddleware` uses the
  explicit `cors_allow_origins` (the frontend origin(s) only) and keeps
  `allow_credentials=True` but narrows `allow_methods` / `allow_headers` to the
  actual set the frontend uses (Part 6.2).
- The frontend `NEXT_PUBLIC_API_BASE_URL` is the public backend origin for that
  environment.

---

## Part 4 — Containerization and repeatable builds (Package 3)

**Status: done.** `backend/Dockerfile` (multi-stage, pinned `uv 0.11.25` copied
from its image, `uv sync --frozen --no-dev`, non-root `app` uid/gid 1001,
`--proxy-headers --forwarded-allow-ips='*'`, `/health` HEALTHCHECK),
`frontend/Dockerfile` (`node:20-alpine`, `output: "standalone"`,
`NEXT_PUBLIC_API_BASE_URL` build arg, non-root `node`), `backend/.dockerignore`
and `frontend/.dockerignore` (no `.env`, no tests/evaluation, no VCS),
`frontend/next.config.ts` gains `output: "standalone"`, and
`docker-compose.prod.yml` (db + MinIO + createbucket + backend running
`alembic upgrade head` then uvicorn + frontend) all landed. `docker compose -f
docker-compose.prod.yml config` validates.

### 4.1 `backend/Dockerfile`

- Base `python:3.12-slim` (matches `requires-python = ">=3.12,<3.13"`).
- Install `uv`; `uv sync --frozen --no-dev` against `uv.lock` for deterministic
  dependencies; no dev group in the runtime image.
- Create and run as a non-root user `app` (fixed UID/GID); app code owned by
  that user; filesystem otherwise read-only-friendly (no writes outside `/tmp`
  now that storage is R2 in production).
- Entrypoint: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers
  --forwarded-allow-ips="*"` (Render injects `$PORT`).
- No secrets in build args or layers. `HEALTHCHECK` hitting `/health`.
- `.dockerignore`: `.env*`, `.venv/`, `__pycache__/`, `.pytest*`, `tests/`,
  `evaluation/`, `.git/`, local `storage/`.

### 4.2 `frontend/Dockerfile`

- Multi-stage: `node:20-alpine` builder runs `npm ci` then `npm run build` with
  `next.config.ts` set to `output: "standalone"`.
- Runtime stage copies `.next/standalone`, `.next/static`, and `public/`; runs
  as the non-root `node` user; `CMD ["node", "server.js"]` on `$PORT`.
- Build-time `NEXT_PUBLIC_API_BASE_URL` is supplied as a build arg per
  environment (it is inlined by Next at build time), so staging and production
  images are built separately from the same revision.
- `.dockerignore`: `node_modules/`, `.next/`, `.env*`, `.git/`.
- Turbopack's native SWC binary is blocked by Smart App Control on the
  developer machine, but the Docker build runs on Linux where `next build`
  works normally; the local pre-flight remains `npx next build --webpack`.

### 4.3 Local production-like path

- `docker-compose.prod.yml` composes `db` (existing Postgres image), `backend`,
  and `frontend` from the production Dockerfiles, wired with a local
  `.env` set to `environment=staging`-like values and `storage_backend=local`
  (or a MinIO service for an S3 smoke) so the images can be exercised offline.
- This file is for verification only and is never the deploy mechanism.

### 4.4 Build-time gates

CI (or the documented local pre-flight) must, before an image is considered
releasable: run the backend `pytest` suite, `npm run lint`, and the frontend
production build. Images are built from a clean checkout; the build fails if
`uv.lock` / `package-lock.json` are stale.

---

## Part 5 — Durable storage and database deployment (Package 4)

**Status: code done; data cutover + managed-DB provisioning are operator
steps.** `app/services/storage/base.py` defines the `FileStorage` ABC
(`location_for`, `save_bytes`, `exists`, `get_bytes`, `delete`) plus
`StorageError` / `StoredFile`; `LocalFileStorage` implements it and gains
`get_bytes`; `app/services/storage/s3.py` adds `S3FileStorage` (aioboto3,
path-style, key-safety mirroring the local resolver, `health_check` for
readiness); `build_storage(settings)` selects the backend and is used by
`deps.py`. The two read call sites (`GET /documents/{id}/file`, extraction
preprocessing) now use `get_bytes` — the download route returns a `Response`
with an ASCII-safe `inline` `Content-Disposition` and no path. `aioboto3` is a
dependency; `moto[s3,server]` is a dev dependency and
`backend/tests/test_storage_s3.py` runs `S3FileStorage` against a local
`ThreadedMotoServer` (offline). `app/database/session.py` pins the pool
(`pool_size=5`, `max_overflow=5`, `pool_recycle=1800`). `config.py` coerces the
managed `postgresql://` URL to `postgresql+asyncpg://` so `render.yaml`'s
`fromDatabase` wiring feeds the async app and `alembic/env.py` unchanged.
`backend/scripts/migrate_uploads_to_s3.py` is the idempotent, DB-write-free,
hash-verifying cutover script. **Operator:** provision the managed databases +
R2 buckets and run the cutover (runbook §1, §5).

### 5.1 Storage interface seam

Today `LocalFileStorage` is used directly in `app/api/deps.py`,
`app/api/documents.py`, and
`app/services/processing/extraction/preprocessing.py`, and `StorageError` is
caught by callers.

- Introduce `app/services/storage/base.py` with a `FileStorage` `Protocol`
  (or ABC) capturing the methods callers already use:
  `location_for`, `save_bytes`, `exists`, `path_for`, `delete`, and the
  `StoredFile` result type. `StorageError` moves here and both backends raise
  it.
- `LocalFileStorage` implements `FileStorage` unchanged.
- Add `S3FileStorage(FileStorage)` for R2:
  - `location_for` keeps the exact `"<document_id>/original.pdf"` shape; the
    stored `documents.file_location` value is unchanged and becomes the object
    key (optionally prefixed by `s3_key_prefix`).
  - `save_bytes` does a single `PutObject` with `ContentType: application/pdf`;
    it is atomic at the object level (no partial object is visible), which
    preserves the "a failed request leaves no half-written original" guarantee.
  - `path_for` has no local path to return. Resolve this by **adding a
    streaming read to the interface** — `open_stream(location) -> async
    bytes iterator` (or `get_bytes`) — and switching the two read call sites
    (`GET /documents/{id}/file`, extraction preprocessing) to it. For local
    storage `open_stream` reads the resolved file; for S3 it is a `GetObject`
    body stream. `path_for` stays on the interface for local-only paths but is
    not used in the S3 configuration. `StorageError` is raised for a missing
    key, exactly as for a missing file.
  - Key-safety mirrors `LocalFileStorage.resolve`: reject absolute, `..`, or
    prefix-escaping locations before building a key.
- `get_storage()` in `deps.py` selects the backend from `settings.storage_backend`
  and constructs it once (module-level singleton, like today). Type hints on
  call sites widen from `LocalFileStorage` to `FileStorage`.
- New dependency: `aioboto3` (async S3 client over `aiobotocore`), added to
  `backend/pyproject.toml` and `uv.lock`. Tests that need S3 semantics use a
  local MinIO container or `moto`; the default test configuration stays
  `storage_backend=local` and asserts no network call.

### 5.2 Data cutover

- Existing local uploads are migrated once with a documented, re-runnable
  script (`backend/scripts/migrate_uploads_to_s3.py` ⚠ name): for every
  `documents` row, read `storage/uploads/<file_location>` and `PutObject` it at
  the same key; verify object size and SHA-256 against the stored
  `file_hash`; report any row whose file is missing or whose hash disagrees;
  make **no** database writes (the key is already correct).
- The script is idempotent (skip keys that already exist with a matching hash)
  and is run as an explicit release step, not on boot.
- Verification: after cutover, `GET /documents`, `GET /documents/{id}`, and
  `GET /documents/{id}/file` return the same bytes and metadata for a sample
  spanning every `documents.status`, and a fresh upload → download round trip
  succeeds.

### 5.3 PostgreSQL deployment

- Render PostgreSQL, one instance per environment. `DATABASE_URL` is injected
  from the managed instance into the backend's environment group as the async
  URL (`postgresql+asyncpg://...`).
- `alembic/env.py` already derives its URL from `settings.database_url`; no
  change needed. If Alembic online mode needs a sync driver in the container,
  document deriving a `psycopg`-scheme URL from the async one at migration
  time rather than storing a second secret.
- Connection pool: set explicit bounds in `app/database/session.py` sized to
  the instance's connection limit and the single-instance assumption
  (⚠ `pool_size=5`, `max_overflow=5`, `pool_pre_ping=True`,
  `pool_recycle=1800`). Revisit if instance count ever exceeds 1.

### 5.4 Migrations as a controlled release step

- Render **Pre-Deploy Command** on the backend service runs
  `alembic upgrade head` against that environment's database before the new
  image receives traffic. A failed migration aborts the deploy and the old
  image keeps serving.
- Every migration in the release span must use an expand/contract shape that is
  backward-compatible with the previous image and have a tested `downgrade`.
  The runbook records whether the downgrade is non-destructive; a destructive
  downgrade is never part of routine rollback and uses backup restoration or a
  forward fix instead.
- Existing `alembic check` drift (two pre-existing Stage 3 index/model
  differences noted in the Stage 8 doc) is documented as known and outside
  Stage 9; Stage 9 adds no migration unless a hardening change requires one.
- Backups are taken immediately before a production migration (Part 8.1).

---

## Part 6 — Edge and application security hardening (Package 5)

This package adds **operational** controls. It does **not** add user
authentication.

**Status: done.** `app/core/middleware.py` adds pure-ASGI
`RequestIDMiddleware` (assigns/propagates `X-Request-ID`, binds it for logging),
`SecurityHeadersMiddleware` (`X-Content-Type-Options`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy`, HSTS when
deployed, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`
except on the docs paths), and `MaxBodySizeMiddleware` (rejects a declared
`Content-Length` over `max_file_size_bytes + 5 MB` with the standard error
envelope). `app/core/rate_limit.py` wires `slowapi` — off by default, on via
`RATE_LIMIT_ENABLED`, `RATE_LIMIT_DEFAULT` globally + `RATE_LIMIT_UPLOAD` on
`POST /documents`, `429` in the standard envelope. `main.py` adds `TrustedHostMiddleware(allowed_hosts=settings.trusted_hosts)`,
narrows CORS to `GET/POST/DELETE/OPTIONS` + `Content-Type`/`X-Request-ID` while
keeping `allow_credentials`, and disables `/docs` `/redoc` `/openapi.json` when
`environment == "production"`. The 20 MB / 10-page / single-PDF / readability
checks are untouched and still server-authoritative. Log/error exposure: the
existing `app/core/errors.py` remains the only error surface (generic 500 with a
correlation id); structured logs carry request metadata only, never invoice
content (Part 7).

### 6.1 HTTPS

Enforced at the Render edge with managed certificates and HTTP→HTTPS redirect.
The backend trusts `X-Forwarded-Proto` only because the edge is the sole
ingress (Part 3.4). HSTS is set via the security-headers middleware (6.3).

### 6.2 CORS and trusted hosts

- `TrustedHostMiddleware` with the explicit `trusted_hosts` list; a request
  with an unlisted `Host` gets a 400 before routing.
- `CORSMiddleware` restricted to the environment's frontend origin(s), with
  `allow_methods` and `allow_headers` narrowed from `*` to the concrete set
  used by the frontend (`GET, POST, DELETE`; `Content-Type`, plus
  `X-Request-ID` if the client sends one). `allow_credentials=True` stays.

### 6.3 Security headers

A small middleware adds, on every response:
`Strict-Transport-Security: max-age=31536000; includeSubDomains`,
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer`, and a minimal
`Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` for API
responses. The frontend (Next.js) sets its own CSP / headers via
`next.config.ts` appropriate to an app that renders PDFs in an iframe / object.

### 6.4 Request and body limits

- The 20 MB / 10-page / single-PDF / structurally-readable checks stay
  **server-authoritative and unchanged** in the document API.
- Add a hard request-body cap at the edge / ASGI layer (⚠ 25 MB) so oversized
  bodies are rejected before buffering, independent of the application check.
- Reject `POST /documents` requests that are not `multipart/form-data` with a
  single file part early (already effectively enforced; make it explicit).

### 6.5 Rate limiting

- `slowapi` (or an equivalent ASGI limiter) keyed by client IP
  (`X-Forwarded-For` left-most, since the edge is trusted).
- Limits from settings: `rate_limit_default` ⚠ `60/minute` on the API in
  general, `rate_limit_upload` ⚠ `10/minute` on `POST /documents`. Exceeding a
  limit returns `429` in the standard API error shape.
- `rate_limit_enabled` defaults `False` (development / tests) and is `True` in
  staging and production.
- Single-instance in-memory storage is acceptable for the MVP; note in the
  runbook that horizontal scaling would need a shared store (Redis).

### 6.6 Logs, errors, and data exposure review

A checklist executed and recorded in this package:

- No log line or error response contains PDF bytes or extracted text, a
  credential, an internal filesystem path, a raw provider request / response,
  or a stack trace. `DEBUG=false` in staging / production; FastAPI docs
  (`/docs`, `/redoc`, `/openapi.json`) are ⚠ disabled in production.
- The existing consistent API error layer (`app/core/errors.py`) is confirmed
  to be the only error surface; unhandled exceptions produce a generic 500 with
  a correlation id and are logged server-side with detail.
- The R2 bucket is private (no public read); PDFs are served only through the
  authenticated-by-obscurity `GET /documents/{id}/file` route streaming from
  storage — no public bucket URL, no pre-signed URL handed to the browser in
  the MVP.

---

## Part 7 — Health, logging, monitoring, alerting (Package 6)

**Status: code done; alert wiring remains operator acceptance work.** `GET /health` is
dependency-free (the platform health-check path). `GET /health/ready` now also
probes storage (`S3FileStorage.health_check` / local dir) and returns `503`
with a per-dependency body when the DB or storage is down.
`app/core/observability.py` provides `configure_logging()` (JSON when
`LOG_FORMAT=json`, every record carries `request_id` + `environment`) and
`configure_sentry()` (no-op unless `SENTRY_DSN` set; `send_default_pii=False`,
`traces_sample_rate=0.1`, a `before_send` scrubber dropping request
bodies/cookies and masking auth-like headers). `RequestIDMiddleware` echoes
`X-Request-ID` on every response. `main.py` mounts
`prometheus-fastapi-instrumentator` at `/metrics` (skipped under `test`) for
standard request rate/latency/error series. `app/core/metrics.py` adds
`ledgerdrop_uploads_total{outcome}`, per-stage duration and failure metrics,
terminal decision outcomes, and an exact review-queue-depth gauge. Alert rules
(§7.4) must be configured in Render/Sentry by the operator.

### 7.1 Liveness vs readiness

- `GET /health` — liveness only. No database, no storage, no provider. Returns
  `{"status": "ok", "app", "environment"}`. This is the Render health-check
  path for the backend.
- `GET /health/ready` — readiness. Checks: `SELECT 1` on the database
  **and** a cheap storage probe (`HeadBucket` / a `location_for` +
  `exists` on a sentinel key for S3; a directory stat for local). Returns
  per-dependency status and a non-200 if any dependency is down. Used by the
  runbook and by the readiness alert, not by the platform health check (so a
  transient dependency blip does not cycle the instance).

### 7.2 Structured logging

- Production `log_format=json`. Each log record carries a timestamp, level,
  logger name, message, `request_id`, and `environment`.
- A middleware reads an inbound `X-Request-ID` or generates one, binds it to a
  context var for the request, echoes it on the response header, and includes
  it on every log line and on the API error body's diagnostic id.
- Request log at completion: method, route template (not the raw path with
  ids where avoidable), status, duration_ms — **no** query string dumps, form
  contents, or headers.

### 7.3 Metrics

⚠ `prometheus-fastapi-instrumentator` exposing `GET /metrics` (scraped by
Render / an external scraper), or, if a scrape target is not available for the
MVP, structured counters emitted to logs and derived there. Metrics:

- `uploads_total` (counter, by outcome accepted/rejected),
- `pipeline_stage_duration_seconds` (histogram, by stage:
  extraction/normalization/validation/decision),
- `pipeline_outcomes_total` (counter, by terminal outcome
  ACCEPTED/NEEDS_REVIEW/FAILED),
- `pipeline_failures_total` (counter, by stage),
- `review_queue_depth` (gauge, documents in `NEEDS_REVIEW`),
- standard request rate / latency / error-rate series.

No metric label carries invoice content, a document id is acceptable only as a
low-cardinality exemplar, not a label.

### 7.4 Error monitoring and alerts

- ⚠ Sentry via `sentry_dsn`, with `send_default_pii=False`, a `before_send`
  scrubber that drops request bodies / form data / the `Authorization` header
  (none today, future-proofing) and any field named like a key, and
  `traces_sample_rate` low (⚠ 0.1).
- Alerts (delivery channel ⚠ email to the operator):
  1. `/health/ready` failing for ⚠ > 3 minutes.
  2. 5xx rate over ⚠ 2% of requests over ⚠ 5 minutes.
  3. `pipeline_failures_total` rate over ⚠ 5 in ⚠ 10 minutes.
  4. p95 `pipeline_stage_duration_seconds` over ⚠ 60 s for ⚠ 10 minutes.
  5. `review_queue_depth` over ⚠ 25 for ⚠ 24 hours (backlog, informational).
- Every alert links to the relevant runbook section.

---

## Part 8 — Recovery, retention, operational controls (Package 7)

**Status: procedures written; execution is operator-only.** Backup schedules,
retention/deletion, restore steps, recovery objectives, and — importantly — the
failed-attempt recovery procedure (which uses the **existing** per-stage retry
routes and never mutates a prior completed attempt row) are in
`docs/stage-9-runbook.md` §6–9. The dry-run-by-default
`scripts/recover_stuck_attempts.py` safely marks confirmed stale `PROCESSING`
attempts as retryable technical failures. The one tested restoration and the
interrupted-attempt drill require the live managed database + R2 bucket and are
the operator's to perform and record (runbook §7).

### 8.1 Backups

- **Database:** Render PostgreSQL automated daily backups; point-in-time
  recovery where the plan provides it (⚠ confirm with the chosen instance
  tier). A manual backup / snapshot is taken immediately before every
  production migration.
- **Files:** R2 does not support S3 bucket versioning. Use a separate,
  access-isolated R2 backup bucket with daily timestamped copies and a
  lifecycle rule retaining snapshots for ⚠ 30 days. Exercise a sample
  restoration quarterly.

### 8.2 Retention and deletion

- Original PDFs and all processing / review / correction records are retained
  for the life of the document row; there is no automatic expiry in the MVP.
- `DELETE /documents/{id}` already removes the row and its live stored object.
  Backup snapshots follow the separately approved retention/purge policy.
  Confirm the live delete path also holds for
  `APPROVED` / `REJECTED` documents (a Stage 7 fix already addressed this).
- A documented, deliberate purge procedure for a specific document (row, live
  object, and backup snapshots) lives in the runbook for data-subject requests ⚠.

### 8.3 Recovery objectives

- ⚠ RPO 24 h (daily backup) — tighter with PITR where available.
- ⚠ RTO 4 h for a full environment rebuild from `render.yaml` + latest backup +
  R2 bucket.

### 8.4 Tested restoration

Once per environment during Stage 9: restore the latest database backup into a
throwaway instance, point a scratch backend at it plus a read copy of the R2
bucket, and confirm `GET /documents`, `GET /documents/{id}/file`, and the
review queue all work. Record the wall-clock time and any manual step; feed
corrections back into Part 10.

### 8.5 Interrupted and failed pipeline attempts

- No new mechanism. Recovery uses the **existing** retry routes:
  a `FAILED` extraction / normalization / validation / decision attempt is
  re-driven through its own `retry` endpoint or `POST /documents/{id}/pipeline`;
  a failed correction chain through the corrections `retry` route.
- An attempt interrupted mid-flight (instance restart during processing) is
  left as its persisted `PROCESSING` / `FAILED` row; the runbook documents
  identifying such rows and re-driving them.
- **No silent mutation of audit history.** Recovery never edits or deletes a
  prior attempt row; it appends a new attempt, exactly as the normal retry
  flow does.

---

## Part 9 — End-to-end staging verification (Package 8)

**Status: harness ready; the run itself needs a live staging stack.**
`backend/scripts/staging_smoke.py` (stdlib-only) does the upload → pipeline →
terminal-outcome → byte-identical re-download → delete round trip against a
deployed backend and is the runbook's smoke step (§3). The evaluation corpus
under `backend/evaluation/` + `expected.json` + `scoring.py` is the corpus for
step 3. The full backend `pytest` suite, `npm run lint`, and the frontend
production build are the regression gates. **Operator:** stand up the staging
stack and execute steps 1–6 below, appending a dated pass/fail checklist here.

Run entirely against the staging stack (production-like images, managed
Postgres, R2 bucket):

1. **Clean-database migration:** `alembic upgrade head` from an empty database
   succeeds; schema matches `Base.metadata` (allowing the two known pre-Stage-3
   drifts).
2. **In-place migration:** restore a copy of the current production (or
   pre-Stage-9) schema+data, run `alembic upgrade head`, confirm no data loss
   and that existing documents remain readable from R2 after the cutover
   script.
3. **Corpus round trip:** process every invoice in `backend/evaluation/invoices/`
   (`digital_basic`, `digital_incomplete`, `digital_multi_line_items`,
   `digital_multi_page`, `digital_unusual_layout`, `low_quality`,
   `not_an_invoice`, `scanned_no_text`) from `POST /documents` through the
   pipeline to a terminal outcome; outcomes match
   `backend/evaluation/expected.json` within its existing tolerances, using
   `EXTRACTION_PROVIDER=fake` for determinism (a small `openai` sample run is
   done separately and manually).
4. **Review + correction flows:** take a `NEEDS_REVIEW` document through
   approve and through reject; submit a correction that re-decides to
   `ACCEPTED` (auto-accept) and one that returns to the queue; confirm
   `documents.status` and the attempt chain match the Stage 7/8 specs.
5. **Regression suites:** full backend `pytest` green against the staging
   database; `npm run lint` clean; frontend production build compiles; the
   deployed frontend renders the dashboard, review, and correction screens
   against the staging backend.
6. **Abuse / failure checks:** oversized body rejected at the edge cap;
   non-PDF and 11-page PDF rejected server-side; rate limit returns `429` past
   the threshold; two concurrent `POST /documents/{id}/pipeline` starts still
   yield one active attempt (`409` on the loser); backend instance restart
   mid-pipeline leaves a recoverable row and no corrupt data.

A dated checklist with pass/fail and evidence links is appended to this
document when the package runs.

---

## Part 10 — Release, rollback, operator runbook (Package 9)

**Status: runbook written (`docs/stage-9-runbook.md`, §0–12); the release and
rollback rehearsals are operator-only** (they need the live Render project) and
are recorded in the runbook's release log when done.

Delivered as `docs/stage-9-runbook.md`, covering at least:

1. **Initial provisioning:** apply `render.yaml`, create the two environment
   groups, create the R2 buckets and scoped tokens, set every secret, first
   deploy of staging then production.
2. **Routine release:** the Part 2.4 checklist; push image / trigger deploy;
   Pre-Deploy migration; readiness wait; Part 10.3 smoke checks;
   announce done.
3. **Smoke checks:** `GET /health`, `GET /health/ready`, upload a known-good
   sample invoice, run the pipeline, confirm the terminal outcome, re-download
   the PDF and byte-compare, load the frontend dashboard.
4. **Rollback:** redeploy the previous image; retain the backward-compatible
   expanded schema by default; apply a recorded downgrade only when explicitly
   verified non-destructive; re-run smoke checks; record the incident.
5. **Backup restoration:** restore database backup to a new instance; restore /
   re-point the R2 bucket; reconfigure and redeploy; verify (Part 8.4 steps).
6. **Incident diagnosis:** reading structured logs by `request_id`, the
   readiness endpoint, metrics, and Sentry; the decision tree from alert →
   likely cause → action.
7. **Failed-job recovery:** finding `FAILED` / stuck `PROCESSING` attempts;
   which retry route applies; the no-audit-mutation rule.
8. **Secret rotation:** database credential, R2 token, OpenAI key — rotate,
   redeploy, verify, revoke old.
9. **Routine maintenance:** dependency updates (`uv.lock`,
   `package-lock.json`), Postgres minor-version upgrades, certificate renewal
   (managed), log / metric retention review.

**Rehearsals before Stage 9 is declared complete:** one full release to
staging following the runbook verbatim, and one full rollback on staging
following the runbook verbatim, each with notes folded back into the runbook.

---

## Part 11 — Completion gate

**The code and documentation prerequisites are met on this revision.** Every
gate below still requires evidence from the live Render + R2 infrastructure;
record results in `docs/stage-9-runbook.md`.

Stage 9 is complete only when **all** hold:

1. The same reviewed revision deploys repeatably to staging and to production
   from `render.yaml` + documented steps, with no manual code edits between
   environments.
2. `alembic upgrade head` has executed successfully against staging. Rollback
   has been rehearsed using the previous application with the compatible
   expanded schema, or a specifically reviewed non-destructive downgrade when
   one exists; destructive downgrade is never required as a routine gate.
3. A database backup **and** the R2 bucket have been restored and verified
   (Part 8.4).
4. Liveness / readiness, structured logs, metrics, and at least the readiness
   and 5xx alerts are live and have been shown to fire on an induced fault.
5. The full backend `pytest` suite, `npm run lint`, and the frontend
   production build are green on the released revision, and the Part 9
   staging verification checklist passed.
6. The MVP acceptance flow — upload → extraction → normalization → validation →
   decision → (review / correction where applicable) → terminal status —
   succeeds in the production-like environment against the evaluation corpus.
7. `CLAUDE.md`, `README.md`, `backend/README.md`, `frontend/README.md`, and
   `docs/stage-9-runbook.md` reflect the deployed reality.

---

## Part 12 — Verification map

| Checklist item | Where it is proven |
|---|---|
| Pinned baseline recorded: platform, environments, topology, region, traffic, availability, cost, ownership, acceptance checklist, rollback conditions | Part 2 of this document |
| `render.yaml` encodes the Part 2 topology — two isolated environments, Docker web services, managed Postgres per env, autodeploy off, backend pre-deploy migration, service secrets as `sync: false` | `render.yaml`; `render blueprints validate render.yaml` (or dashboard validation) once Package 2/3 land |
| No Stage 2–8 API contract, schema, or lifecycle change | full backend `pytest` green on the released revision; API diff review |
| Existing documents readable and unchanged after storage + DB cutover | Part 5.2 cutover verification + Part 9 step 2 |
| `environment` restricted to `development\|test\|staging\|production`; `check_deployment_safety` aborts a deployed boot on debug, localhost/loopback DB, localhost CORS, wildcard hosts, missing S3 / OpenAI config, non-JSON logs; every problem reported at once | `backend/tests/test_config.py` (`test_deployed_check_*`) with a synthetic deployed config |
| `check_deployment_safety` error names only settings, never a secret value (it is a post-model `DeploymentConfigError`, not a pydantic validator) | `test_config.py::test_deployed_check_error_does_not_leak_secret_values` |
| `DATABASE_URL` driver-less `postgresql://` / `postgres://` coerced to `postgresql+asyncpg://`; a URL already naming a driver untouched | `test_config.py::test_database_url_scheme_is_coerced_to_asyncpg` |
| Secrets absent from the repo and from image layers | `.gitignore` (`!.env.production.example` re-include is documentation only) + `.dockerignore` review; `render.yaml` uses `sync: false`; `.env.production.example` files hold placeholders only; image inspection in CI |
| Backend and frontend images build reproducibly, run as non-root, contain no dev deps | `backend/Dockerfile` (`--no-dev`, non-root `app`), `frontend/Dockerfile` (non-root `node`), `.dockerignore` × 2 — **done**; a Linux `docker build` + `docker run` smoke is the operator's pre-release gate (runbook §2) |
| `FileStorage` seam: `LocalFileStorage` and `S3FileStorage` both satisfy the ABC; `deps.py` / `documents.py` / `preprocessing.py` depend on `FileStorage`; `build_storage` selects by `STORAGE_BACKEND` | `tests/test_storage.py`, `tests/test_storage_s3.py` (`test_build_storage_selects_backend_from_settings`) — **done** |
| S3 `save_bytes` is a single visible object (no partial); `_key_for` rejects `..` / absolute / drive / backslash | `tests/test_storage_s3.py::test_key_for_rejects_unsafe_locations` — **done** |
| Read path uses `get_bytes` for both backends; `GET /documents/{id}/file` returns a path-free `inline` response, byte-identical | `tests/test_storage_s3.py`, `tests/test_documents_read.py`, `tests/test_extraction_preprocessing.py` — **done**; live byte-compare in `staging_smoke.py` |
| `migrate_uploads_to_s3.py` is idempotent, verifies size + SHA-256, makes no DB writes | script exists (`backend/scripts/`); dry-run + re-run on staging (runbook §5) — **operator** |
| `alembic upgrade head` clean from empty DB and from current schema; every release-span migration has a working `downgrade` | Part 9 steps 1–2; Part 10 rollback rehearsal |
| Pre-Deploy migration aborts the deploy on failure and leaves the old image serving | induced-failure test on staging |
| `TrustedHostMiddleware(allowed_hosts=trusted_hosts)` + narrowed CORS (`GET/POST/DELETE/OPTIONS`, `Content-Type`/`X-Request-ID`) + security headers (`nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, COOP, CSP `default-src 'none'`, HSTS when deployed) on every response | `main.py`, `app/core/middleware.py` — **done** (full backend suite green through the new middleware); HTTPS enforced at the Render edge |
| 20 MB / 10-page / single-PDF / readability checks unchanged and server-authoritative; `MaxBodySizeMiddleware` rejects an over-`Content-Length` request with the standard envelope | existing Stage 2 tests (green) + `app/core/middleware.py` — **done** |
| Rate limiting returns `429` in the error envelope past the threshold; off by default (dev/tests) | `app/core/rate_limit.py`, `@limiter.limit(UPLOAD_RATE_LIMIT)` on `POST /documents`; live check in runbook §3 — **done (disabled path proven by the suite)** |
| No PDF content, credential, path, provider payload, or stack trace in logs or error bodies; `/docs` off in production | `errors.py` unchanged (green); JSON logs carry request metadata only; `docs_url=None` when `environment=="production"` — **done** |
| R2 bucket is private; PDFs served only via the app route | runbook §1 (private bucket, no public URL); anonymous object fetch fails — **operator** |
| `/health` has no dependencies; `/health/ready` reports DB + storage and returns `503` when a dependency is down | `tests/test_health.py::test_ready_checks_database_and_storage` — **done** |
| Every log record carries `request_id`; `X-Request-ID` echoed on the response | `app/core/observability.py` `RequestIdFilter` + `RequestIDMiddleware` — **done** |
| `/metrics` exposes standard request rate/latency/error series + `ledgerdrop_uploads_total{outcome}`; no content labels | `main.py` instrumentator + `documents.py` counter — **done**; per-stage pipeline + queue-depth metrics deferred |
| Readiness and 5xx alerts fire on an induced fault | Part 11 item 4 rehearsal |
| Database backup + R2 bucket restored and verified; wall-clock recorded | Part 8.4 |
| Interrupted / failed attempts recovered via existing retry routes with no audit-row mutation | recovery drill on staging; attempt-chain assertions |
| One release rehearsal and one rollback rehearsal completed on staging per the runbook | Part 10 rehearsal notes |
| MVP acceptance flow succeeds in the production-like environment on the evaluation corpus | Part 9 step 3 + Part 11 item 6 |

---

## Appendix A — provisional (⚠) values register

These are the project owner's to confirm before the package that consumes each
one. Until confirmed, the listed default is used and is marked ⚠ in the text.

| Value | Provisional default | Consumed by |
|---|---|---|
| Render instance sizes | Starter (staging), Standard (production) | Part 2.1, Package 1 |
| Instance count | 1 backend, 1 frontend, 1 DB per env | Parts 2.1, 5.3, 6.5 |
| Primary region | Render Oregon (US West) | Part 2.3, Package 1 |
| Data residency commitment | United States, none contractual | Part 2.3 |
| Expected traffic | tens of uploads/day, single-digit concurrent reviewers | Part 2.3 |
| Availability target | best-effort business hours, no SLA | Part 2.3 |
| Cost ceiling | unset — owner to set before provisioning | Part 2.3 |
| Operator / on-call | the project owner | Part 2.3 |
| Domains | `staging.<domain>` / `<domain>` or Render hosts | Part 2.2 |
| Rollback non-recovery window | 15 minutes | Part 2.5 |
| Rate limits | `60/minute` general, `10/minute` upload | Parts 3.1, 6.5 |
| Edge request-body cap | `max_file_size_bytes + 5 MB` (~25 MB at the 20 MB limit) | Part 6.4 |
| `/docs` in production | disabled | Part 6.6 |
| Pool settings | `pool_size=5`, `max_overflow=5`, `pool_recycle=1800` | Part 5.3 |
| Metrics stack | `prometheus-fastapi-instrumentator` + `/metrics` | Part 7.3 |
| Error monitoring | Sentry, `traces_sample_rate=0.1`, PII off | Part 7.4 |
| Alert thresholds | readiness > 3 min; 5xx > 2% / 5 min; pipeline failures > 5 / 10 min; p95 stage > 60 s / 10 min; queue > 25 / 24 h | Part 7.4 |
| Alert channel | email to the operator | Part 7.4 |
| Backup RPO / RTO | 24 h / 4 h | Part 8.3 |
| R2 backup-snapshot retention | 30 days | Part 8.1 |
| Second file copy (cross-bucket) | deferred | Part 8.1 |
| Data-subject purge procedure | documented, manual | Part 8.2 |
| Upload-migration script name | `backend/scripts/migrate_uploads_to_s3.py` | Part 5.2 |
