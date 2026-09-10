"""Regression coverage for the Stage 9 HTTP edge hardening."""

from __future__ import annotations

import re

import pytest

from app.core.config import settings
from app.main import _MAX_REQUEST_BODY_BYTES


@pytest.mark.asyncio
async def test_valid_request_id_is_echoed_and_security_headers_are_set(client):
    response = await client.get("/health", headers={"X-Request-ID": "caller-123._:x"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "caller-123._:x"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'"
    )


@pytest.mark.asyncio
async def test_unsafe_request_id_is_replaced(client):
    response = await client.get("/health", headers={"X-Request-ID": "unsafe value"})

    request_id = response.headers["x-request-id"]
    assert request_id != "unsafe value"
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)


@pytest.mark.asyncio
async def test_document_pdf_can_be_embedded_by_configured_frontend(client):
    response = await client.get(
        "/documents/00000000-0000-0000-0000-000000000000/file"
    )

    # The row need not exist to verify headers: middleware wraps the 404 too.
    assert response.status_code == 404
    assert "x-frame-options" not in response.headers
    csp = response.headers["content-security-policy"]
    assert csp.startswith("frame-ancestors ")
    for origin in settings.cors_allow_origins:
        assert origin in csp


@pytest.mark.asyncio
async def test_oversized_declared_body_is_rejected_with_stable_error(client):
    response = await client.post(
        "/documents",
        content=b"x",
        headers={"Content-Length": str(_MAX_REQUEST_BODY_BYTES + 1)},
    )

    assert response.status_code == 413
    assert response.json() == {
        "error": {
            "code": "REQUEST_TOO_LARGE",
            "message": "The request body is too large.",
        }
    }
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])
    assert response.headers["x-content-type-options"] == "nosniff"
