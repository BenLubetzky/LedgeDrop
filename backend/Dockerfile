# syntax=docker/dockerfile:1
#
# Production image for the LedgerDrop FastAPI backend (Stage 9, Package 3).
# Build context is the backend/ directory (see render.yaml `dockerContext`).
#
#   docker build -t ledgerdrop-backend ./backend
#
# The image contains no secrets and no dev dependencies. Configuration and
# credentials are supplied at runtime by the environment; a staging/production
# process refuses to start unless that configuration is deploy-safe
# (app/core/config.py :: check_deployment_safety).

# ----- builder: resolve and install dependencies into a venv -----------------
FROM python:3.12-slim-bookworm AS builder

# Pinned uv, copied from its published image (no network install step).
COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer first: only invalidated when the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Application code.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ----- runtime: minimal image, non-root user --------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Fixed uid/gid non-root user.
RUN groupadd --system --gid 1001 app \
 && useradd --system --uid 1001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# The resolved venv and the app, owned by the runtime user.
COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

# Render injects $PORT; default to 8000 for local runs.
EXPOSE 8000

# Liveness probe for local / compose use. Render uses `healthCheckPath` instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request,sys; url='http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health'; sys.exit(0 if urllib.request.urlopen(url, timeout=3).status==200 else 1)"]

# --proxy-headers + trust all forwarded IPs: the Render edge is the only ingress,
# so client scheme/host come from X-Forwarded-*.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
