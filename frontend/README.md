# LedgerDrop frontend

Minimal Next.js interface for the FastAPI backend.

- **`/`** — upload invoice PDFs, run extraction, and browse the document list.
- **`/review`** — the human-review queue: invoices the Stage 6 decision routed
  to `NEEDS_REVIEW`, oldest first, each showing why it was flagged.
- **`/review/[documentId]`** — the review screen: the original PDF beside the
  extracted and normalized field values, the validation findings, and the
  ordered Stage 6 decision reasons, with **Approve** / **Reject** actions
  (a name is always required; a note is required to reject). Only client-safe
  values are shown — no raw provider payloads, finding `context`, hashes, or
  internal paths.

## Local development

```bash
cp .env.example .env.local
npm install
npm run dev
```

The frontend runs at `http://localhost:3000`. The backend URL defaults to
`http://localhost:8000` and can be changed with `NEXT_PUBLIC_API_BASE_URL`.

On a Windows machine where Smart App Control / Application Control blocks Next's
unsigned native SWC binary (`@next/swc-win32-x64-msvc`), Turbopack cannot load;
run `npx next build --webpack` and `npx next dev --webpack` instead. The WASM
fallback still type-checks and compiles.

## Layout

```text
src/
  app/
    layout.tsx                 root layout + metadata
    globals.css                all styles (plain CSS + custom properties)
    page.tsx                   -> DocumentDashboard
    review/
      page.tsx                 -> ReviewQueue
      [documentId]/page.tsx    -> ReviewDetail
  components/
    document-dashboard.tsx     upload + document list + extraction panel
    review-queue.tsx           GET /reviews/queue
    review-detail.tsx          resolves the extraction/normalization/validation/
                               decision chain via the `latest` endpoints, then
                               POSTs the review
  lib/
    api.ts                     API base URL, fetch helper, error/format helpers
    review-types.ts            response shapes + shared field labels
```
