"""Post-deploy smoke test: upload -> pipeline -> terminal outcome -> download.

Runs against a deployed backend over HTTP, using only the standard library so
it works from any checkout (no dev dependencies). It:

  1. GET /health and /health/ready
  2. POST /documents with a sample invoice PDF
  3. POST /documents/{id}/pipeline and check a terminal decision/validation
  4. GET /documents/{id}/file and byte-compare with what was uploaded
  5. DELETE /documents/{id} to leave no residue

Usage::

    uv run python -m scripts.staging_smoke --base-url https://ledgerdrop-backend-staging.onrender.com

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

_DEFAULT_SAMPLE = Path(__file__).resolve().parents[1] / "evaluation" / "invoices" / "digital_basic.pdf"


def _request(method: str, url: str, *, body: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # noqa: PERF203
        return exc.code, exc.read()


def _multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"----ledgerdrop{uuid.uuid4().hex}"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/pdf\r\n\r\n",
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


def _check(label: str, ok: bool, detail: str = "") -> None:
    mark = "ok  " if ok else "FAIL"
    print(f"  [{mark}] {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="e.g. https://host (no trailing slash)")
    parser.add_argument("--sample", type=Path, default=_DEFAULT_SAMPLE)
    parser.add_argument("--keep", action="store_true", help="do not delete the test document")
    args = parser.parse_args(argv)

    base = args.base_url.rstrip("/")
    pdf = args.sample.read_bytes()

    print(f"smoke: {base}")

    status, raw = _request("GET", f"{base}/health")
    _check("GET /health", status == 200, f"status {status}")

    status, raw = _request("GET", f"{base}/health/ready")
    ready = json.loads(raw or b"{}")
    _check(
        "GET /health/ready",
        status == 200 and ready.get("database") == "ok" and ready.get("storage") == "ok",
        json.dumps(ready),
    )

    body, content_type = _multipart("file", "smoke.pdf", pdf)
    status, raw = _request(
        "POST", f"{base}/documents", body=body, headers={"Content-Type": content_type}
    )
    _check("POST /documents", status == 201, f"status {status}")
    document = json.loads(raw)
    document_id = document["id"] if "id" in document else document["document_id"]

    status, raw = _request(
        "POST",
        f"{base}/documents/{document_id}/pipeline",
        body=b"{}",
        headers={"Content-Type": "application/json"},
    )
    _check("POST /documents/{id}/pipeline", status in (200, 201), f"status {status}")
    result = json.loads(raw)
    extraction_status = (result.get("extraction") or {}).get("status")
    _check("pipeline extraction completed", extraction_status == "COMPLETED", str(extraction_status))
    validation_status = (result.get("validation") or {}).get("status")
    _check("pipeline validation completed", validation_status == "COMPLETED", str(validation_status))
    decision = result.get("decision") or {}
    _check(
        "pipeline reached a terminal decision",
        decision.get("status") == "COMPLETED"
        and decision.get("outcome") in {"ACCEPTED", "NEEDS_REVIEW"},
        json.dumps(decision),
    )

    status, downloaded = _request("GET", f"{base}/documents/{document_id}/file")
    _check(
        "GET /documents/{id}/file byte-identical",
        status == 200 and downloaded == pdf,
        f"status {status}, {len(downloaded)} bytes",
    )

    if not args.keep:
        status, _ = _request("DELETE", f"{base}/documents/{document_id}")
        _check("DELETE /documents/{id}", status in (200, 204), f"status {status}")

    print("smoke: PASS")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
