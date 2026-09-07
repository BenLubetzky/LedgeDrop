"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  API_BASE_URL,
  codeFromBody,
  errorMessage,
  formatConfidence,
  formatDateTime,
  getJson,
  messageFromBody,
  readJson,
} from "@/lib/api";
import {
  fieldPathLabel,
  SCALAR_FIELDS,
  type DecisionResult,
  type DocumentRecord,
  type ExtractionResult,
  type NormalizationResult,
  type ValidationResult,
} from "@/lib/review-types";

type Chain = {
  document: DocumentRecord;
  extraction: ExtractionResult;
  normalization: NormalizationResult;
  validation: ValidationResult;
  decision: DecisionResult;
};

type Resolved = { action: "APPROVE" | "REJECT"; reviewer: string };

function humanize(code: string | null): string | null {
  if (!code) return null;
  const words = code.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const SUBMIT_ERROR_BY_CODE: Record<string, string> = {
  DECISION_ALREADY_REVIEWED:
    "This invoice has already been reviewed. Refresh the queue for the latest list.",
  DECISION_NOT_REVIEWABLE:
    "This invoice is no longer awaiting review. Refresh the queue.",
  STALE_DECISION_SOURCE:
    "This invoice has been reprocessed since it was queued and can no longer be reviewed here.",
};

export function ReviewDetail({ documentId }: { documentId: string }) {
  const [chain, setChain] = useState<Chain | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const [reviewer, setReviewer] = useState("");
  const [note, setNote] = useState("");
  const [action, setAction] = useState<"APPROVE" | "REJECT" | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [resolved, setResolved] = useState<Resolved | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const doc = await getJson<DocumentRecord>(`/documents/${documentId}`);
      if (doc.status !== "NEEDS_REVIEW") {
        setChain(null);
        setLoadError(
          `This invoice is ${doc.status
            .replace("_", " ")
            .toLowerCase()} and is not awaiting review.`,
        );
        return;
      }
      const extraction = await getJson<ExtractionResult>(
        `/documents/${documentId}/extractions/latest`,
      );
      const normalization = await getJson<NormalizationResult>(
        `/documents/${documentId}/extractions/${extraction.extraction_id}/normalizations/latest`,
      );
      const validation = await getJson<ValidationResult>(
        `/documents/${documentId}/extractions/${extraction.extraction_id}` +
          `/normalizations/${normalization.normalization_id}/validations/latest`,
      );
      const decision = await getJson<DecisionResult>(
        `/documents/${documentId}/extractions/${extraction.extraction_id}` +
          `/normalizations/${normalization.normalization_id}` +
          `/validations/${validation.validation_id}/decisions/latest`,
      );
      setChain({
        document: doc,
        extraction,
        normalization,
        validation,
        decision,
      });
    } catch (err) {
      setLoadError(errorMessage(err, "This invoice could not be loaded."));
    } finally {
      setIsLoading(false);
    }
  }, [documentId]);

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(initial);
  }, [load]);

  const errorsByPath = useMemo(() => {
    const map = new Map<string, string>();
    chain?.normalization.data.errors.forEach((e) =>
      map.set(e.field_path, e.message),
    );
    return map;
  }, [chain]);

  const trimmedReviewer = reviewer.trim();
  const trimmedNote = note.trim();

  async function submit(chosen: "APPROVE" | "REJECT") {
    if (!chain || isSubmitting) return;
    if (trimmedReviewer.length === 0) return;
    if (chosen === "REJECT" && trimmedNote.length === 0) return;
    const verb = chosen === "APPROVE" ? "approve" : "reject";
    if (
      !window.confirm(
        `Are you sure you want to ${verb} this invoice? This decision is final.`,
      )
    ) {
      return;
    }
    setAction(chosen);
    setIsSubmitting(true);
    setSubmitError(null);
    const { extraction, normalization, validation, decision } = chain;
    const path =
      `/documents/${documentId}/extractions/${extraction.extraction_id}` +
      `/normalizations/${normalization.normalization_id}` +
      `/validations/${validation.validation_id}` +
      `/decisions/${decision.decision_id}/review`;
    try {
      const response = await fetch(`${API_BASE_URL}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: chosen,
          reviewer_name: trimmedReviewer,
          note: trimmedNote.length > 0 ? trimmedNote : null,
        }),
      });
      const body = await readJson(response);
      if (!response.ok) {
        const code = codeFromBody(body);
        throw new Error(
          (code && SUBMIT_ERROR_BY_CODE[code]) ||
            messageFromBody(body, "The decision could not be recorded."),
        );
      }
      setResolved({ action: chosen, reviewer: trimmedReviewer });
    } catch (err) {
      setSubmitError(errorMessage(err, "The decision could not be recorded."));
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="app-shell review-shell">
      <header className="topbar">
        <Link className="brand" href="/" aria-label="LedgerDrop workspace">
          <span className="brand-mark" aria-hidden="true">
            L
          </span>
          <span className="brand-name">LedgerDrop</span>
        </Link>
        <Link className="stage-label back-link" href="/review">
          ← Review queue
        </Link>
      </header>

      {isLoading && (
        <div className="review-page">
          <div className="list-message" role="status">
            <span className="spinner" aria-hidden="true" />
            <p>Loading the invoice…</p>
          </div>
        </div>
      )}

      {!isLoading && loadError && (
        <div className="review-page">
          <div className="list-message" role="alert">
            <p>{loadError}</p>
            <Link href="/review">Back to the queue</Link>
          </div>
        </div>
      )}

      {!isLoading && chain && (
        <div className="review-detail">
          <section className="pdf-pane" aria-label="Original PDF">
            <iframe
              title={`Original PDF for ${chain.document.original_filename}`}
              src={`${API_BASE_URL}/documents/${documentId}/file`}
            />
            <a
              className="pdf-fallback"
              href={`${API_BASE_URL}/documents/${documentId}/file`}
              target="_blank"
              rel="noreferrer"
            >
              Open the PDF in a new tab
            </a>
          </section>

          <section className="detail-pane">
            <header className="detail-head">
              <p className="eyebrow">Needs review</p>
              <h1 title={chain.document.original_filename}>
                {chain.document.original_filename}
              </h1>
              <p className="detail-sub">
                Uploaded {formatDateTime(chain.document.uploaded_at)}
              </p>
            </header>

            {resolved ? (
              <div
                className={`resolution-banner is-${resolved.action.toLowerCase()}`}
                role="status"
              >
                <strong>
                  {resolved.action === "APPROVE" ? "Approved" : "Rejected"}
                </strong>
                <span>
                  Recorded by {resolved.reviewer}. This invoice has left the
                  review queue.
                </span>
                <Link className="resolution-link" href="/review">
                  Back to the queue
                </Link>
              </div>
            ) : (
              <form
                className="decision-form"
                onSubmit={(event) => event.preventDefault()}
              >
                <label className="field-label" htmlFor="reviewer-name">
                  Your name
                </label>
                <input
                  id="reviewer-name"
                  className="text-input"
                  value={reviewer}
                  onChange={(event) => setReviewer(event.target.value)}
                  maxLength={200}
                  autoComplete="name"
                  placeholder="Recorded with the decision for the audit trail"
                />

                <label className="field-label" htmlFor="review-note">
                  Note{" "}
                  <span className="field-hint">
                    (required to reject, optional to approve)
                  </span>
                </label>
                <textarea
                  id="review-note"
                  className="text-input note-input"
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  maxLength={4000}
                  rows={3}
                  placeholder="Why is this invoice being approved or rejected?"
                />

                {submitError && (
                  <p className="form-error" role="alert">
                    {submitError}
                  </p>
                )}

                <div className="decision-actions">
                  <button
                    type="button"
                    className="btn-approve"
                    onClick={() => void submit("APPROVE")}
                    disabled={isSubmitting || trimmedReviewer.length === 0}
                  >
                    {isSubmitting && action === "APPROVE"
                      ? "Approving…"
                      : "Approve"}
                  </button>
                  <button
                    type="button"
                    className="btn-reject"
                    onClick={() => void submit("REJECT")}
                    disabled={
                      isSubmitting ||
                      trimmedReviewer.length === 0 ||
                      trimmedNote.length === 0
                    }
                  >
                    {isSubmitting && action === "REJECT"
                      ? "Rejecting…"
                      : "Reject"}
                  </button>
                </div>
                <p className="decision-hint">
                  A decision is final and moves the invoice out of the queue.
                </p>
              </form>
            )}

            <section className="detail-section">
              <h2>Why this invoice needs review</h2>
              {chain.decision.data && chain.decision.data.reasons.length > 0 ? (
                <ul className="reason-list">
                  {chain.decision.data.reasons.map((reason, index) => (
                    <li
                      key={`${reason.code}-${index}`}
                      className={
                        reason.triggers_review
                          ? "reason-row is-gating"
                          : "reason-row"
                      }
                    >
                      <span className="reason-tag">
                        {reason.triggers_review ? "Review" : "Note"}
                      </span>
                      <div>
                        <p>{reason.message}</p>
                        {(humanize(reason.source_rule) ||
                          fieldPathLabel(reason.field_path)) && (
                          <p className="reason-meta">
                            {[
                              humanize(reason.source_rule),
                              fieldPathLabel(reason.field_path),
                            ]
                              .filter(Boolean)
                              .join(" · ")}
                          </p>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">No decision reasons were recorded.</p>
              )}
            </section>

            <section className="detail-section">
              <h2>
                Validation findings{" "}
                <span className="count-pill">
                  {chain.validation.data.findings.length}
                </span>
              </h2>
              {chain.validation.data.findings.length > 0 ? (
                <ul className="finding-list">
                  {chain.validation.data.findings.map((finding, index) => (
                    <li
                      key={`${finding.rule}-${index}`}
                      className={`finding-row sev-${finding.severity}`}
                    >
                      <span className="sev-tag">{finding.severity}</span>
                      <div>
                        <p>{finding.message}</p>
                        <p className="finding-meta">
                          {[
                            humanize(finding.rule),
                            fieldPathLabel(finding.field_path),
                            finding.expected !== null
                              ? `expected ${finding.expected}`
                              : null,
                            finding.actual !== null
                              ? `found ${finding.actual}`
                              : null,
                          ]
                            .filter(Boolean)
                            .join(" · ")}
                        </p>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">No validation findings.</p>
              )}
            </section>

            <section className="detail-section">
              <h2>Invoice fields</h2>
              <div className="values-table-wrap">
                <table className="values-table">
                  <thead>
                    <tr>
                      <th>Field</th>
                      <th>Extracted</th>
                      <th>Normalized</th>
                    </tr>
                  </thead>
                  <tbody>
                    {SCALAR_FIELDS.map(([key, label]) => {
                      const extracted = chain.extraction.data[key];
                      const normalized = chain.normalization.data[key];
                      const fieldError = errorsByPath.get(key);
                      const confidence = formatConfidence(extracted.confidence);
                      return (
                        <tr key={key}>
                          <th scope="row">{label}</th>
                          <td>
                            {extracted.value ?? <span className="muted">—</span>}
                            {confidence && (
                              <span className="cell-hint">{confidence}</span>
                            )}
                          </td>
                          <td>
                            {fieldError ? (
                              <span className="cell-error">{fieldError}</span>
                            ) : (
                              normalized ?? <span className="muted">—</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>

            <section className="detail-section">
              <h2>
                Line items{" "}
                <span className="count-pill">
                  {Math.max(
                    chain.extraction.data.line_items.length,
                    chain.normalization.data.line_items.length,
                  )}
                </span>
              </h2>
              {Math.max(
                chain.extraction.data.line_items.length,
                chain.normalization.data.line_items.length,
              ) > 0 ? (
                <div className="values-table-wrap">
                  <table className="values-table line-table">
                    <thead>
                      <tr>
                        <th>Line / source</th>
                        <th>Description</th>
                        <th>Qty</th>
                        <th>Unit price</th>
                        <th>Line total</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Array.from({
                        length: Math.max(
                          chain.extraction.data.line_items.length,
                          chain.normalization.data.line_items.length,
                        ),
                      }).flatMap((_, index) => {
                        const extracted = chain.extraction.data.line_items[index];
                        const normalized = chain.normalization.data.line_items[index];
                        const fields = [
                          "description",
                          "quantity",
                          "unit_price",
                          "line_total",
                        ] as const;
                        return [
                          <tr key={`${index}-extracted`}>
                            <th scope="row">Line {index + 1} · Extracted</th>
                            {fields.map((field) => {
                              const value = extracted?.[field];
                              const confidence = value
                                ? formatConfidence(value.confidence)
                                : null;
                              return (
                                <td key={field}>
                                  {value?.value ?? <span className="muted">—</span>}
                                  {confidence && (
                                    <span className="cell-hint">{confidence}</span>
                                  )}
                                </td>
                              );
                            })}
                          </tr>,
                          <tr key={`${index}-normalized`}>
                            <th scope="row">Line {index + 1} · Normalized</th>
                            {fields.map((field) => (
                              <td key={field}>
                                {normalized?.[field] ?? (
                                  <span className="muted">—</span>
                                )}
                              </td>
                            ))}
                          </tr>,
                        ];
                      })}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="muted">No line items.</p>
              )}
            </section>
          </section>
        </div>
      )}
    </main>
  );
}
