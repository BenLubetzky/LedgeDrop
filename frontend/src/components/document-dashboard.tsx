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
                  <span className={`status status-${document.status.toLowerCase()}`}>
                    {document.status.replace("_", " ")}
                  </span>
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
    </main>
  );
}
