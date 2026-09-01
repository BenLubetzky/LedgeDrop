"""The error envelope is the same shape regardless of how the error arose."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import NotFoundError, register_exception_handlers


async def test_unknown_route_uses_error_envelope(client: AsyncClient) -> None:
    response = await client.get("/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "NOT_FOUND"
    assert isinstance(body["error"]["message"], str)


async def test_method_not_allowed_uses_error_envelope(client: AsyncClient) -> None:
    response = await client.post("/health")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


@pytest.fixture
async def app_with_failing_route() -> AsyncClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("internal detail that must not leak")

    @app.get("/missing")
    async def missing() -> None:
        raise NotFoundError("No such widget.", code="WIDGET_NOT_FOUND")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def test_unexpected_error_is_masked(app_with_failing_route: AsyncClient) -> None:
    response = await app_with_failing_route.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "internal detail" not in body["error"]["message"]


async def test_api_error_subclass_controls_code_and_message(
    app_with_failing_route: AsyncClient,
) -> None:
    response = await app_with_failing_route.get("/missing")
    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "WIDGET_NOT_FOUND",
        "message": "No such widget.",
    }
