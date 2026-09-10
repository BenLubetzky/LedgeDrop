# syntax=docker/dockerfile:1
#
# Production image for the LedgerDrop Next.js frontend (Stage 9, Package 3).
# Build context is the frontend/ directory (see render.yaml `dockerContext`).
#
#   docker build --build-arg NEXT_PUBLIC_API_BASE_URL=https://api.example.com \
#     -t ledgerdrop-frontend ./frontend
#
# NEXT_PUBLIC_API_BASE_URL is inlined into the client bundle at build time, so
# staging and production images are built separately from the same revision.

# ----- deps: install node_modules from the lockfile -------------------------
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ----- builder: produce the standalone server ------------------------------
FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ARG NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production

RUN npm run build

# ----- runtime: minimal image, non-root user ------------------------------
FROM node:20-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# `output: "standalone"` emits a self-contained server plus a pruned
# node_modules under .next/standalone; static assets are copied separately.
COPY --from=builder --chown=node:node /app/public ./public
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static

USER node

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["node", "-e", "require('http').get('http://127.0.0.1:'+(process.env.PORT||3000)+'/',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"]

CMD ["node", "server.js"]
