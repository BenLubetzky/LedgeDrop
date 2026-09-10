# LedgerDrop operator runbook (Stage 9, Package 9)

Operational procedures for the Render + Cloudflare R2 deployment defined in
`docs/stage-9-deployment-readiness.md` and `render.yaml`. Two environments,
`staging` and `production`, fully isolated.

Values marked ⚠ are provisional (see the spec's Appendix A). This runbook is
kept in step with `render.yaml` and the backend configuration; a change to
either that affects a procedure below is part of that change.

---

## 0. Prerequisites (one-time, per operator)

- Render account with access to the LedgerDrop project; `render` CLI logged in.
- Cloudflare account with R2 enabled.
- `git`, `docker`, `uv`, and `psql` locally for rehearsals and recovery.
- The environment's secrets to hand: R2 endpoint + access key id + secret,
  OpenAI API key, (optional) Sentry DSN.

---

## 1. Initial provisioning

Do **staging first**, verify it end to end (section 8 of the spec), then repeat
for production.

1. **R2 buckets.** In the Cloudflare dashboard create
   `ledgerdrop-uploads-staging` (and later `ledgerdrop-uploads`). Keep the
   bucket **private** (no public access, no public dev URL). R2 does not support
   S3 bucket versioning, so also create an isolated backup bucket and schedule
   the daily timestamped copy described in section 6 with ⚠ 30-day retention.
   Create a scoped **R2 API token** limited to the live bucket with
   object read/write; record the Access Key ID, Secret Access Key, and the S3
   endpoint `https://<account-id>.r2.cloudflarestorage.com`.
2. **Blueprint.** From a clean checkout of the release revision:
   validate with `render blueprints validate render.yaml`, then use the Render
   dashboard → New → Blueprint → this repo.
   Render creates the databases, environment groups, and services from
   `render.yaml`. For a staging-only first pass you may comment out the
   `production` block, launch, then restore it.
3. **Secrets.** Render prompts for every `sync: false` var at launch. Set, on
   the backend service for each environment: `S3_ENDPOINT_URL`,
   `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `OPENAI_API_KEY`, and
   `SENTRY_DSN` (blank if not using Sentry yet). `DATABASE_URL` is wired
   automatically from the managed database.
4. **Confirm non-secret config.** Check the environment group has
   `ENVIRONMENT`, `DEBUG=false`, `LOG_FORMAT=json`, `CORS_ALLOW_ORIGINS`
   (the real frontend origin, no localhost), `TRUSTED_HOSTS` (the real backend
   host, no `*`), `STORAGE_BACKEND=s3`, `S3_BUCKET`, `EXTRACTION_PROVIDER=openai`.
   If any is wrong the backend will refuse to boot (`check_deployment_safety`) —
   that is intended; fix the value and redeploy.
5. **First deploy.** Trigger a manual deploy of the backend, then the frontend
   (frontend build bakes in `NEXT_PUBLIC_API_BASE_URL`, so it must point at the
   now-live backend). The backend Pre-Deploy Command runs `alembic upgrade head`
   against an empty database.
6. **First-time data move** (only when migrating an existing local dataset):
   run section 5.
7. **Smoke** (section 3). Record the deploy in the release log.

---

## 2. Routine release

**Pre-flight (must all pass — spec Part 2.4):**

- `main` is green: `cd backend && uv run pytest`; `cd frontend && npm run lint`
  and the production build compiles.
- `alembic upgrade head` applies on a clone of the target DB, and the matching
  `alembic downgrade <base>` for this release's migration span is written down
  (section 4) and was exercised once on staging.
- The images build clean from a clean checkout with no secret in any layer.
- The runbook reflects any new deploy/migration/rollback step.

**Deploy:**

1. Merge to `main`; tag the release (⚠ `vYYYY.MM.DD` or similar).
2. Render → backend service → **Manual Deploy** → select the commit. The
   Pre-Deploy Command runs `alembic upgrade head`; a migration failure aborts
   the deploy and the previous image keeps serving.
3. Wait for the health check to pass, then **Manual Deploy** the frontend at
   the same commit.
4. Run the smoke checks (section 3).
5. Watch logs / metrics / alerts for ⚠ 15 minutes. If the rollback conditions
   (spec Part 2.5) are met and a quick forward fix is not obvious, roll back
   (section 4).
6. Note commit, migration span, and result in the release log.

---

## 3. Smoke checks (post-deploy)

Against the environment's public URLs:

1. `GET /health` → `200`, `{"status":"ok", ...}`.
2. `GET /health/ready` → `200`, `database` and `storage` both `ok`.
3. Upload a known-good sample invoice and run it to a terminal outcome:
   `uv run python -m scripts.staging_smoke --base-url https://<backend-host>`
   (uploads `backend/evaluation/invoices/digital_basic.pdf`, runs the pipeline,
   asserts a terminal outcome, re-downloads the PDF and byte-compares, then
   deletes the document). Exit code 0 = pass.
4. Load the frontend dashboard, the `/review` queue, and open one document.

Any failure → treat as a failed release (section 4).

---

## 4. Rollback

Trigger when, after a deploy: `/health/ready` stays down past the grace
period; the smoke path fails; or the error-rate / p95-latency alert fires and
does not recover within ⚠ 15 minutes; or a data-integrity problem is seen.

1. **Code.** Render → backend service → **Rollback** to the previous deploy
   (or Manual Deploy of the last-good commit). Do the same for the frontend.
2. **Schema.** Keep the expanded, backward-compatible schema by default. Apply
   `alembic downgrade <previous-head>` only when that exact downgrade was tested
   on a staging clone and recorded as non-destructive. Otherwise use a forward
   fix or restore the pre-migration backup. Never improvise a production
   downgrade during an incident.
3. Re-run the smoke checks (section 3).
4. Record the incident: what broke, what was rolled back, follow-up.

A forward fix is preferred when the fault is understood and small.

---

## 5. Local uploads → R2 migration (one-time per environment)

Only needed when an environment inherits an existing local `storage/uploads/`
dataset. It makes **no database writes** (`file_location` is already the object
key) and is safe to re-run.

1. Stop writes (put the frontend in maintenance or accept a brief gap).
2. From a checkout with the environment's S3 config exported and
   `STORAGE_BACKEND=s3`:
   `cd backend && uv run python -m scripts.migrate_uploads_to_s3 --dry-run --local-dir /path/to/storage/uploads`
   Review the report (uploaded / already-present / missing-local / hash-mismatch).
3. Re-run without `--dry-run`. Every object is re-read after upload and its
   SHA-256 re-checked against `documents.file_hash`. Exit code 1 ⇒ at least one
   missing/mismatched file — investigate before switching `STORAGE_BACKEND`.
4. Set `STORAGE_BACKEND=s3` on the backend and redeploy.
5. Verify: `GET /documents`, `GET /documents/{id}`, `GET /documents/{id}/file`
   for a sample spanning every `documents.status`; then a fresh upload →
   download round trip.

---

## 6. Backups

- **Database.** Render PostgreSQL takes automated daily backups; point-in-time
  recovery where the plan provides it (⚠ confirm on the chosen tier). Take a
  **manual logical export immediately before every production migration**
  (Render dashboard → database → Recovery → Create export). Confirm the export
  finishes before deploying the migration.
- **Files.** R2 does not implement S3 bucket versioning. Maintain a separate,
  access-isolated backup bucket and copy live objects daily under a dated
  prefix; retain snapshots for ⚠ 30 days with an R2 lifecycle rule. Quarterly,
  restore a sample into a scratch bucket and byte-compare its SHA-256.
- Recovery objectives: ⚠ RPO 24 h (tighter with PITR), ⚠ RTO 4 h for a full
  environment rebuild.

---

## 7. Backup restoration

Rehearse once per environment during Stage 9, and any time before relying on it.

1. From Render's database **Recovery** page, restore to a point before the
   incident (where PITR is available), or import a verified logical export into
   a **new** database (never overwrite the live one blind).
2. Bring up a scratch backend (a one-off Render service or local `uv run
   uvicorn`) pointed at the restored `DATABASE_URL` and a **read copy** of the
   R2 bucket (or the live bucket read-only).
3. Verify: `GET /documents`, `GET /documents/{id}/file` for several rows, and
   the `/reviews/queue`.
4. Record wall-clock time and every manual step; fold fixes back here.
5. To cut over for real: repoint the live backend's `DATABASE_URL` at the
   restored instance (or promote it) and redeploy; re-run smoke checks.

---

## 8. Incident diagnosis

1. **Scope.** `GET /health` (process up?) and `GET /health/ready` (DB /
   storage reachable?).
2. **Logs.** Render → service → Logs. Every line is JSON with `request_id`,
   `level`, `logger`, `environment`. Filter by `request_id` to follow one
   request end to end. No invoice content is logged — do not expect payloads.
3. **Metrics.** `/metrics` (or Render metrics): request rate, latency, 5xx
   rate; `ledgerdrop_uploads_total{outcome=...}`.
4. **Errors.** Sentry (if configured) — issues are scrubbed of request bodies,
   cookies, and auth-like headers.
5. **Alert → likely cause:**
   - readiness failing → DB or R2 unreachable; check the managed DB status and
     the R2 token/endpoint; a bad config redeploy → check the last deploy.
   - 5xx spike right after a deploy → roll back (section 4).
   - pipeline-failure spike, health green → provider outage (OpenAI) or a bad
     input batch; check extraction error codes; failed attempts are retriable
     (section 9).
   - p95 latency high → provider latency or DB pressure; check pool saturation
     and provider status.

---

## 9. Failed / stuck processing-job recovery

Failed-attempt recovery uses the existing per-stage retry routes and never
deletes or rewrites a completed attempt row. The recovery command changes only
stale `PROCESSING` attempts to a retryable technical `FAILED` state.

- A `FAILED` extraction / normalization / validation / decision attempt:
  re-drive it via its own `.../retry` route, or re-run the whole chain with
  `POST /documents/{id}/pipeline`.
- A `FAILED` correction chain: the corrections `.../retry` route.
- For attempts left `PROCESSING` after an instance restart, first stop or scale
  down every backend worker that could still own work. Preview attempts older
  than the incident boundary: `uv run python -m scripts.recover_stuck_attempts
  --older-than-minutes 30`. Review every printed ID, then repeat with `--apply`.
  Use `--stage extraction|normalization|validation|decision|correction` to
  narrow the operation. Restart workers, then use the applicable retry route.
- The command is dry-run by default, locks selected rows, skips concurrently
  locked rows, preserves completed history and partial correction-chain IDs,
  and records `WORKER_INTERRUPTED`. Never run `--apply` while an old worker may
  still be completing the selected rows.
- Do **not** hand-edit `documents.status` or any `invoice_*` row to "unstick" a
  job. The retry routes are the only supported path.

---

## 10. Secret rotation

For each of: database credential, R2 API token, OpenAI API key, Sentry DSN.

1. Create the new credential at the source (Render DB, Cloudflare R2, OpenAI,
   Sentry).
2. Update non-secret shared configuration in the Render environment group, or
   update the secret on the backend service where Stage 9 keeps credentials.
3. Redeploy the backend (secrets are read at boot).
4. `GET /health/ready` → `200` with `storage` `ok`; run an upload→download
   smoke.
5. Revoke the old credential at the source.
6. Record the rotation (what, when, by whom).

Rotating `NEXT_PUBLIC_API_BASE_URL` or any frontend build-time value requires a
frontend **rebuild + redeploy**, not just a restart.

---

## 11. Routine maintenance

- **Dependencies.** Periodically `uv lock --upgrade` (backend) and
  `npm update` / `npm audit` (frontend) on a branch; run the full suites; deploy
  through staging.
- **PostgreSQL.** Apply Render's minor-version upgrades in a maintenance window;
  take a manual backup first.
- **TLS.** Managed by Render — nothing to do beyond keeping the custom domain's
  DNS pointed at Render.
- **Logs / metrics retention.** Review the retention window ⚠ quarterly.
- **R2.** Check the scheduled backup copy and its snapshot lifecycle; watch
  both buckets' size vs. the cost ceiling ⚠.

---

## 12. Release log (append-only)

| Date | Env | Commit | Migration span | Result | Notes |
|---|---|---|---|---|---|
| _first entry at initial provisioning_ | | | | | |
