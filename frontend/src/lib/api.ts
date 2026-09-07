// Shared helpers for talking to the FastAPI backend. Kept framework-free so
// both server and client components can import the constants and formatters.

export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"
).replace(/\/$/, "");

type ApiErrorBody = { error?: { message?: string; code?: string } };

/** Best-effort JSON parse; returns null instead of throwing on an empty body. */
export async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/** Pull the client-safe message out of the standard `{ error: { message } }` envelope. */
export function messageFromBody(body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    const candidate = body as ApiErrorBody;
    if (candidate.error?.message) return candidate.error.message;
  }
  return fallback;
}

export function codeFromBody(body: unknown): string | null {
  if (body && typeof body === "object") {
    const candidate = body as ApiErrorBody;
    if (candidate.error?.code) return candidate.error.code;
  }
  return null;
}

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

const NETWORK_FALLBACK =
  "Could not reach LedgerDrop. Make sure the backend is running.";

/** GET `path` and parse it, raising a client-safe Error on any failure. */
export async function getJson<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { cache: "no-store" });
  } catch {
    throw new Error(NETWORK_FALLBACK);
  }
  const body = await readJson(response);
  if (!response.ok) {
    throw new Error(messageFromBody(body, "The request could not be completed."));
  }
  return body as T;
}

export function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("en", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function formatConfidence(value: string | null): string | null {
  if (value === null) return null;
  const pct = Math.round(Number(value) * 100);
  return Number.isFinite(pct) ? `${pct}% confidence` : null;
}
