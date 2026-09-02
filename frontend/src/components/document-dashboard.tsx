"use client";

import {
  ChangeEvent,
  DragEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"
).replace(/\/$/, "");
const MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024;

type DocumentStatus =
  | "UPLOADED"
  | "PROCESSING"
  | "COMPLETED"
  | "NEEDS_REVIEW"
  | "FAILED";

type DocumentRecord = {
  document_id: string;
  original_filename: string;
  file_size_bytes: number;
  page_count: number;
  status: DocumentStatus;
  uploaded_at: string;
  updated_at: string;
};

type ApiErrorBody = { error?: { message?: string } };

type ExtractedField = {
  value: string | null;
  confidence: string | null;
};

type ExtractionData = {
  invoice_number: ExtractedField;
  invoice_date: ExtractedField;
  due_date: ExtractedField;
  vendor_name: ExtractedField;
  vendor_tax_id: ExtractedField;
  customer_name: ExtractedField;
  currency: ExtractedField;
  subtotal: ExtractedField;
  tax_amount: ExtractedField;
  total_amount: ExtractedField;
  line_items: Array<{
    description: ExtractedField;
    quantity: ExtractedField;
    unit_price: ExtractedField;
    line_total: ExtractedField;
  }>;
};

type ExtractionResult = {
  extraction_id: string;
  document_id: string;
  attempt_number: number;
  status: "PROCESSING" | "COMPLETED" | "FAILED";
  provider_name: string;
  provider_model: string | null;
  started_at: string;
  completed_at: string | null;
  failure_code: string | null;
  failure_message: string | null;
  data: ExtractionData;
};

const EXTRACTION_FIELDS: Array<[keyof Omit<ExtractionData, "line_items">, string]> = [
  ["invoice_number", "Invoice number"],
  ["invoice_date", "Invoice date"],
  ["due_date", "Due date"],
  ["vendor_name", "Vendor"],
  ["vendor_tax_id", "Vendor tax ID"],
  ["customer_name", "Customer"],
  ["currency", "Currency"],
  ["subtotal", "Subtotal"],
  ["tax_amount", "Tax amount"],
  ["total_amount", "Total amount"],
];

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function messageFromBody(body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    const candidate = body as ApiErrorBody;
    if (candidate.error?.message) return candidate.error.message;
  }
  return fallback;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function validateFile(file: File): string | null {
  const hasPdfExtension = file.name.toLowerCase().endsWith(".pdf");
  const hasPdfMimeType = file.type === "application/pdf" || file.type === "";

  if (!hasPdfExtension || !hasPdfMimeType) return "Choose a PDF file.";
  if (file.size === 0) return "The selected file is empty.";
  if (file.size > MAX_FILE_SIZE_BYTES) {
    return "The selected PDF is larger than 20 MB.";
  }
  return null;
}

function uploadFile(
  file: File,
  onProgress: (percentage: number) => void,
): Promise<DocumentRecord> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const formData = new FormData();
    formData.append("file", file);
    request.open("POST", `${API_BASE_URL}/documents`);
    request.responseType = "json";

    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        onProgress(Math.min(99, Math.round((event.loaded / event.total) * 100)));
      }
    });
    request.addEventListener("load", () => {
      if (request.status >= 200 && request.status < 300) {
        onProgress(100);
        resolve(request.response as DocumentRecord);
      } else {
        reject(
          new Error(
            messageFromBody(request.response, "The PDF could not be uploaded."),
          ),
        );
      }
    });
    request.addEventListener("error", () => {
      reject(
        new Error(
          "Could not reach LedgerDrop. Make sure the backend is running.",
        ),
      );
    });
    request.addEventListener("abort", () => {
      reject(new Error("The upload was cancelled."));
    });
    request.send(formData);
  });
}

function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("en", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatConfidence(value: string | null): string {
  if (value === null) return "";
  return `${Math.round(Number(value) * 100)}% confidence`;
}

function UploadIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5M5 14v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4" />
    </svg>
  );
}

function FileIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M7 3h7l4 4v14H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z" />
      <path d="M14 3v5h4M9 13h6M9 17h4" />
    </svg>
  );
}

function ArrowIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5 12h14m-5-5 5 5-5 5" />
    </svg>
  );
}

export function DocumentDashboard() {
  const inputRef = useRef<HTMLInputElement>(null);
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [isLoadingDocuments, setIsLoadingDocuments] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [activeFilename, setActiveFilename] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [pendingRemovalId, setPendingRemovalId] = useState<string | null>(null);
  const [removingDocumentId, setRemovingDocumentId] = useState<string | null>(null);
  const [extractingDocumentId, setExtractingDocumentId] = useState<string | null>(null);
  const [selectedExtraction, setSelectedExtraction] = useState<ExtractionResult | null>(null);
  const [selectedDocumentName, setSelectedDocumentName] = useState<string | null>(null);

  const loadDocuments = useCallback(async () => {
    setListError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/documents`, {
        cache: "no-store",
      });
      const body = await readJson(response);
      if (!response.ok) {
        throw new Error(
          messageFromBody(body, "The document list could not be loaded."),
        );
      }
      setDocuments(body as DocumentRecord[]);
    } catch (error) {
      setListError(
        errorMessage(
          error,
          "Could not reach LedgerDrop. Make sure the backend is running.",
        ),
      );
    } finally {
      setIsLoadingDocuments(false);
    }
  }, []);

  useEffect(() => {
    const initialLoad = window.setTimeout(() => void loadDocuments(), 0);
    return () => window.clearTimeout(initialLoad);
  }, [loadDocuments]);

  useEffect(() => {
    if (!uploadError) return;
    const dismissTimer = window.setTimeout(() => setUploadError(null), 3000);
    return () => window.clearTimeout(dismissTimer);
  }, [uploadError]);

  useEffect(() => {
    if (!successMessage) return;
    const dismissTimer = window.setTimeout(() => setSuccessMessage(null), 3000);
    return () => window.clearTimeout(dismissTimer);
  }, [successMessage]);

  const startUpload = useCallback(async (file: File) => {
    const validationError = validateFile(file);
    setUploadError(validationError);
    setSuccessMessage(null);
    if (validationError) return;

    setIsUploading(true);
    setActiveFilename(file.name);
    setUploadProgress(0);
    try {
      const created = await uploadFile(file, setUploadProgress);
      setDocuments((current) => [
        created,
        ...current.filter(
          (document) => document.document_id !== created.document_id,
        ),
      ]);
      setSuccessMessage(`${created.original_filename} was uploaded successfully.`);
    } catch (error) {
      setUploadError(errorMessage(error, "The PDF could not be uploaded."));
    } finally {
      setIsUploading(false);
      setActiveFilename(null);
      if (inputRef.current) inputRef.current.value = "";
    }
  }, []);

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) void startUpload(file);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsDragging(false);
    if (isUploading) return;
    const file = event.dataTransfer.files?.[0];
    if (file) void startUpload(file);
  };

  const removeDocument = async (document: DocumentRecord) => {
    setRemovingDocumentId(document.document_id);
    setUploadError(null);
    setSuccessMessage(null);
    try {
      const response = await fetch(
        `${API_BASE_URL}/documents/${document.document_id}`,
        { method: "DELETE" },
      );
      if (!response.ok) {
        const body = await readJson(response);
        throw new Error(messageFromBody(body, "The document could not be removed."));
      }
      setDocuments((current) =>
        current.filter((item) => item.document_id !== document.document_id),
      );
      setSuccessMessage(`${document.original_filename} was removed.`);
    } catch (error) {
      setUploadError(errorMessage(error, "The document could not be removed."));
    } finally {
      setPendingRemovalId(null);
      setRemovingDocumentId(null);
    }
  };

  const showExtraction = async (document: DocumentRecord) => {
    setExtractingDocumentId(document.document_id);
    setUploadError(null);
    try {
      const response = await fetch(
        `${API_BASE_URL}/documents/${document.document_id}/extractions/latest`,
        { cache: "no-store" },
      );
      const body = await readJson(response);
      if (!response.ok) {
        throw new Error(messageFromBody(body, "The extraction could not be loaded."));
      }
      setSelectedDocumentName(document.original_filename);
      setSelectedExtraction(body as ExtractionResult);
    } catch (error) {
      setUploadError(errorMessage(error, "The extraction could not be loaded."));
    } finally {
      setExtractingDocumentId(null);
    }
  };

  const runExtraction = async (document: DocumentRecord) => {
    const isRetry = document.status === "FAILED";
    setExtractingDocumentId(document.document_id);
    setUploadError(null);
    setSuccessMessage(null);
    setDocuments((current) =>
      current.map((item) =>
        item.document_id === document.document_id
          ? { ...item, status: "PROCESSING" }
          : item,
      ),
    );

    try {
      const suffix = isRetry ? "/retry" : "";
      const response = await fetch(
        `${API_BASE_URL}/documents/${document.document_id}/extractions${suffix}`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
      );
      const body = await readJson(response);
      if (!response.ok) {
        throw new Error(messageFromBody(body, "The extraction could not be started."));
      }

      const result = body as ExtractionResult;
      setDocuments((current) =>
        current.map((item) =>
          item.document_id === document.document_id
            ? { ...item, status: result.status }
            : item,
        ),
      );
      if (result.status === "COMPLETED") {
        setSelectedDocumentName(document.original_filename);
        setSelectedExtraction(result);
        setSuccessMessage(`${document.original_filename} was extracted successfully.`);
      } else if (result.status === "FAILED") {
        setUploadError(result.failure_message ?? "The extraction failed. You can retry it.");
      }
    } catch (error) {
      await loadDocuments();
      setUploadError(errorMessage(error, "The extraction could not be started."));
    } finally {
      setExtractingDocumentId(null);
    }
  };

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#main-content" aria-label="LedgerDrop home">
          <span className="brand-mark" aria-hidden="true">L</span>
          <span className="brand-name">LedgerDrop</span>
        </a>
        <span className="stage-label">Invoice workspace</span>
      </header>

      <div className="workspace" id="main-content">
        <section className="intro" aria-labelledby="page-title">
          <div className="intro-title">
            <p className="eyebrow">Invoice workspace</p>
            <h1 id="page-title">Upload and organize invoices</h1>
          </div>
          <p>
            Add English invoice PDFs for secure storage and future processing.
          </p>
        </section>

        <section className="upload-card" aria-labelledby="upload-title">
          <div
            className={`dropzone${isDragging ? " is-dragging" : ""}${
              isUploading ? " is-disabled" : ""
            }`}
            onDragEnter={(event) => {
              event.preventDefault();
              if (!isUploading) setIsDragging(true);
            }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node)) {
                setIsDragging(false);
              }
            }}
            onDrop={handleDrop}
          >
            <input
              ref={inputRef}
              id="invoice-file"
              className="file-input"
              type="file"
              accept="application/pdf,.pdf"
              onChange={handleFileChange}
              disabled={isUploading}
            />
            <div className="upload-icon"><UploadIcon /></div>
            <div>
              <h2 id="upload-title">
                {isDragging ? "Drop your PDF here" : "Drop your invoice PDF"}
              </h2>
              <p>PDF only · up to 20 MB · maximum 10 pages</p>
            </div>
            <label className="choose-button" htmlFor="invoice-file">
              {isUploading ? "Uploading…" : "Choose PDF"}
            </label>
          </div>

          {isUploading && (
            <div className="upload-progress" aria-live="polite">
              <div className="progress-copy">
                <span className="truncate">Uploading {activeFilename}</span>
                <span>{uploadProgress}%</span>
              </div>
              <div
                className="progress-track"
                role="progressbar"
                aria-label={`Uploading ${activeFilename}`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={uploadProgress}
              >
                <span style={{ width: `${uploadProgress}%` }} />
              </div>
            </div>
          )}

        </section>

        <section className="documents-section" aria-labelledby="documents-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Uploads</p>
              <h2 id="documents-title">Recent documents</h2>
            </div>
            <button
              className="refresh-button"
              type="button"
              onClick={() => {
                setIsLoadingDocuments(true);
                void loadDocuments();
              }}
              disabled={isLoadingDocuments}
            >
              {isLoadingDocuments ? "Refreshing…" : "Refresh"}
            </button>
          </div>

          {listError && (
            <div className="list-message" role="alert">
              <p>{listError}</p>
              <button type="button" onClick={() => void loadDocuments()}>
                Try again
              </button>
            </div>
          )}
          {!listError && isLoadingDocuments && (
            <div className="list-message" role="status">
              <span className="spinner" aria-hidden="true" />
              <p>Loading documents…</p>
            </div>
          )}
          {!listError && !isLoadingDocuments && documents.length === 0 && (
            <div className="empty-state">
              <div className="empty-icon"><FileIcon /></div>
              <h3>No documents yet</h3>
              <p>Your uploaded invoice PDFs will appear here.</p>
            </div>
          )}
          {!listError && documents.length > 0 && (
            <div className="document-list">
              {documents.map((document) => (
                <article className="document-row" key={document.document_id}>
                  <div className="file-badge"><FileIcon /></div>
                  <div className="document-primary">
                    <h3 title={document.original_filename}>
                      {document.original_filename}
                    </h3>
                    <p>
                      {formatBytes(document.file_size_bytes)} · {document.page_count}{" "}
                      {document.page_count === 1 ? "page" : "pages"} ·{" "}
                      {formatDate(document.uploaded_at)}
                    </p>
                  </div>
                  <span className="document-state-actions">
                    <span className={`status status-${document.status.toLowerCase()}`}>
                      {document.status.replace("_", " ")}
                    </span>
                    {document.status === "COMPLETED" ? (
                      <button
                        className="extraction-button"
                        type="button"
                        onClick={() => void showExtraction(document)}
                        disabled={extractingDocumentId === document.document_id}
                      >
                        {extractingDocumentId === document.document_id ? "Loading…" : "View extraction"}
                      </button>
                    ) : (
                      <button
                        className="extraction-button"
                        type="button"
                        onClick={() => void runExtraction(document)}
                        disabled={
                          document.status === "PROCESSING" ||
                          extractingDocumentId === document.document_id
                        }
                      >
                        {document.status === "PROCESSING" || extractingDocumentId === document.document_id
                          ? "Extracting…"
                          : document.status === "FAILED"
                            ? "Retry extraction"
                            : "Extract"}
                      </button>
                    )}
                  </span>
                  {pendingRemovalId === document.document_id ? (
                    <span className="remove-confirmation">
                      <span>Remove?</span>
                      <button
                        className="remove-confirm-button"
                        type="button"
                        onClick={() => void removeDocument(document)}
                        disabled={removingDocumentId === document.document_id}
                        aria-label={`Confirm removal of ${document.original_filename}`}
                      >
                        {removingDocumentId === document.document_id ? "Removing…" : "Yes"}
                      </button>
                      <button
                        className="remove-cancel-button"
                        type="button"
                        onClick={() => setPendingRemovalId(null)}
                        disabled={removingDocumentId === document.document_id}
                      >
                        No
                      </button>
                    </span>
                  ) : (
                    <button
                      className="remove-button"
                      type="button"
                      onClick={() => setPendingRemovalId(document.document_id)}
                      aria-label={`Remove ${document.original_filename}`}
                    >
                      Remove
                    </button>
                  )}
                  <a
                    className="view-link"
                    href={`${API_BASE_URL}/documents/${document.document_id}/file`}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={`View ${document.original_filename}`}
                  >
                    View <ArrowIcon />
                  </a>
                </article>
              ))}
            </div>
          )}
        </section>
      </div>

      <div className="toast-region" aria-atomic="true">
        {uploadError && (
          <div className="notice notice-error" role="alert">
            <span aria-hidden="true">!</span><p>{uploadError}</p>
          </div>
        )}
        {successMessage && (
          <div className="notice notice-success" role="status">
            <span aria-hidden="true">✓</span><p>{successMessage}</p>
          </div>
        )}
      </div>

      {selectedExtraction && (
        <div
          className="extraction-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setSelectedExtraction(null);
          }}
        >
          <section
            className="extraction-panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="extraction-title"
          >
            <header className="extraction-header">
              <div>
                <p className="eyebrow">Extracted invoice</p>
                <h2 id="extraction-title">{selectedDocumentName}</h2>
              </div>
              <button
                className="panel-close"
                type="button"
                onClick={() => setSelectedExtraction(null)}
                aria-label="Close extraction details"
              >
                Close
              </button>
            </header>

            <div className="extraction-fields">
              {EXTRACTION_FIELDS.map(([key, label]) => {
                const field = selectedExtraction.data[key];
                return (
                  <div className="extraction-field" key={key}>
                    <span>{label}</span>
                    <strong>{field.value ?? "Not found"}</strong>
                    {field.confidence !== null && (
                      <small>{formatConfidence(field.confidence)}</small>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="line-items-section">
              <div className="line-items-heading">
                <h3>Line items</h3>
                <span>{selectedExtraction.data.line_items.length} items</span>
              </div>
              {selectedExtraction.data.line_items.length === 0 ? (
                <p className="no-line-items">No line items were found.</p>
              ) : (
                <div className="line-items-table-wrap">
                  <table className="line-items-table">
                    <thead>
                      <tr><th>Description</th><th>Quantity</th><th>Unit price</th><th>Total</th></tr>
                    </thead>
                    <tbody>
                      {selectedExtraction.data.line_items.map((item, index) => (
                        <tr key={`${selectedExtraction.extraction_id}-${index}`}>
                          <td>{item.description.value ?? "—"}</td>
                          <td>{item.quantity.value ?? "—"}</td>
                          <td>{item.unit_price.value ?? "—"}</td>
                          <td>{item.line_total.value ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
