# LedgerDrop frontend

Minimal Next.js interface for uploading invoice PDFs and viewing the current
document list from the FastAPI backend.

## Local development

```bash
cp .env.example .env.local
npm install
npm run dev
```

The frontend runs at `http://localhost:3000`. The backend URL defaults to
`http://localhost:8000` and can be changed with `NEXT_PUBLIC_API_BASE_URL`.
