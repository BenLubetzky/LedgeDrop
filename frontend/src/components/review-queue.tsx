"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { errorMessage, formatDateTime, getJson } from "@/lib/api";
import type { QueueEntry } from "@/lib/review-types";

function ArrowIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5 12h14m-5-5 5 5-5 5" />
    </svg>
  );
}

export function ReviewQueue() {
  const [entries, setEntries] = useState<QueueEntry[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setEntries(await getJson<QueueEntry[]>("/reviews/queue"));
    } catch (err) {
      setError(errorMessage(err, "The review queue could not be loaded."));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(initial);
  }, [load]);

  return (
    <main className="app-shell review-shell">
      <header className="topbar">
        <Link className="brand" href="/" aria-label="LedgerDrop workspace">
          <span className="brand-mark" aria-hidden="true">
            L
          </span>
          <span className="brand-name">LedgerDrop</span>
        </Link>
        <span className="stage-label">Review queue</span>
      </header>

      <div className="review-page">
        <Link className="back-link" href="/">
          &larr; Back to uploads
        </Link>
        <section className="section-heading review-heading">
          <div>
            <p className="eyebrow">Human review</p>
            <h1>Invoices awaiting a decision</h1>
          </div>
          <button
            className="refresh-button"
            type="button"
            onClick={() => {
              setIsLoading(true);
              void load();
            }}
            disabled={isLoading}
          >
            {isLoading ? "Refreshing…" : "Refresh"}
          </button>
        </section>

        {error && (
          <div className="list-message" role="alert">
            <p>{error}</p>
            <button type="button" onClick={() => void load()}>
              Try again
            </button>
          </div>
        )}

        {!error && isLoading && (
          <div className="list-message" role="status">
            <span className="spinner" aria-hidden="true" />
            <p>Loading the review queue…</p>
          </div>
        )}

        {!error && !isLoading && entries.length === 0 && (
          <div className="empty-state">
            <h3>Nothing to review</h3>
            <p>
              Invoices the decision stage routes to human review will appear
              here.
            </p>
          </div>
        )}

        {!error && entries.length > 0 && (
          <ol className="queue-list">
            {entries.map((entry) => (
              <li className="queue-card" key={entry.decision_id}>
                <div className="queue-card-main">
                  <h2 title={entry.original_filename}>
                    {entry.original_filename}
                  </h2>
                  <p className="queue-meta">
                    Flagged {formatDateTime(entry.decided_at)}
                  </p>
                  <ul className="reason-chips">
                    {entry.decision.reasons.map((reason, index) => (
                      <li
                        key={`${entry.decision_id}-${reason.code}-${index}`}
                        className={
                          reason.triggers_review
                            ? "reason-chip is-gating"
                            : "reason-chip"
                        }
                      >
                        {reason.message}
                      </li>
                    ))}
                  </ul>
                </div>
                <Link
                  className="queue-open"
                  href={`/review/${entry.document_id}`}
                >
                  Review invoice <ArrowIcon />
                </Link>
              </li>
            ))}
          </ol>
        )}
      </div>
    </main>
  );
}
