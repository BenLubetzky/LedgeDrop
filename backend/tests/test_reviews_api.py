"""Tests for the Stage 7 review API endpoints (package 2).

Drive the real HTTP layer through the ``client`` fixture. A ``NEEDS_REVIEW``
decision is reached with five real per-stage calls, the last one carrying
``manual_review_requested: true`` so the deterministic clean fake invoice still
routes to review.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient

from app.api.deps import get_extractor
from app.services.processing.extraction.fake import FakeExtractionProvider

from tests._helpers import make_pdf


@pytest.fixture(autouse=True)
def _offline_extractor(app):
    app.dependency_overrides[get_extractor] = lambda: FakeExtractionProvider()
    yield


async def _needs_review_decision(client: AsyncClient) -> dict[str, str]:
    """Upload -> extract -> normalize -> validate -> decide (manual review).

    Returns a dict of all five ids plus the decision-scoped review base URL.
    """
    up = await client.post(
        "/documents", files={"file": ("invoice.pdf", make_pdf(1), "application/pdf")}
    )
    assert up.status_code == 201, up.text
    document_id = up.json()["document_id"]

    ex = await client.post(f"/documents/{document_id}/extractions")
    extraction_id = ex.json()["extraction_id"]
    no = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}/normalizations"
    )
    normalization_id = no.json()["normalization_id"]
    va = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations"
    )
    validation_id = va.json()["validation_id"]
    de = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations/{validation_id}/decisions",
        json={"manual_review_requested": True},
    )
    assert de.status_code == 201, de.text
    assert de.json()["outcome"] == "NEEDS_REVIEW"
    decision_id = de.json()["decision_id"]

    base = (
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations/{validation_id}"
        f"/decisions/{decision_id}/review"
    )
    return {
        "document_id": document_id,
        "extraction_id": extraction_id,
        "normalization_id": normalization_id,
        "validation_id": validation_id,
        "decision_id": decision_id,
        "base": base,
    }


async def _accepted_decision(client: AsyncClient) -> dict[str, str]:
    """The same chain but decided without manual review -> ACCEPTED."""
    up = await client.post(
        "/documents", files={"file": ("invoice.pdf", make_pdf(1), "application/pdf")}
    )
    document_id = up.json()["document_id"]
    ex = await client.post(f"/documents/{document_id}/extractions")
    extraction_id = ex.json()["extraction_id"]
    no = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}/normalizations"
    )
    normalization_id = no.json()["normalization_id"]
    va = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations"
    )
    validation_id = va.json()["validation_id"]
    de = await client.post(
        f"/documents/{document_id}/extractions/{extraction_id}"
        f"/normalizations/{normalization_id}/validations/{validation_id}/decisions"
    )
    assert de.json()["outcome"] == "ACCEPTED"
    return {
        "base": (
            f"/documents/{document_id}/extractions/{extraction_id}"
            f"/normalizations/{normalization_id}/validations/{validation_id}"
            f"/decisions/{de.json()['decision_id']}/review"
        ),
        "document_id": document_id,
    }


async def _status(client: AsyncClient, document_id: str) -> str:
    return (await client.get(f"/documents/{document_id}")).json()["status"]


# --- submit ------------------------------------------------------


async def test_approve_returns_201_and_moves_the_document(client: AsyncClient) -> None:
    ctx = await _needs_review_decision(client)
    resp = await client.post(
        ctx["base"], json={"action": "APPROVE", "reviewer_name": "  Dana Ops  "}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["action"] == "APPROVE"
    assert body["reviewer_name"] == "Dana Ops"  # trimmed
    assert body["note"] is None
    assert body["document_id"] == ctx["document_id"]
    assert body["decision_id"] == ctx["decision_id"]
    assert set(body) == {
        "review_id",
        "decision_id",
        "document_id",
        "action",
        "reviewer_name",
        "note",
        "policy_version",
        "reviewed_at",
        "created_at",
    }
    assert await _status(client, ctx["document_id"]) == "APPROVED"


async def test_reject_requires_a_note(client: AsyncClient) -> None:
    ctx = await _needs_review_decision(client)
    resp = await client.post(
        ctx["base"], json={"action": "REJECT", "reviewer_name": "Dana Ops"}
    )
    assert resp.status_code == 422
    assert await _status(client, ctx["document_id"]) == "NEEDS_REVIEW"


async def test_reject_with_a_note_moves_the_document(client: AsyncClient) -> None:
    ctx = await _needs_review_decision(client)
    resp = await client.post(
        ctx["base"],
        json={"action": "REJECT", "reviewer_name": "Dana Ops", "note": "duplicate"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["note"] == "duplicate"
    assert await _status(client, ctx["document_id"]) == "REJECTED"


async def test_empty_and_unknown_bodies_are_422(client: AsyncClient) -> None:
    ctx = await _needs_review_decision(client)
    assert (await client.post(ctx["base"], json={})).status_code == 422
    assert (
        await client.post(
            ctx["base"],
            json={"action": "APPROVE", "reviewer_name": "Dana", "role": "admin"},
        )
    ).status_code == 422


async def test_submitting_twice_conflicts(client: AsyncClient) -> None:
    ctx = await _needs_review_decision(client)
    first = await client.post(
        ctx["base"], json={"action": "APPROVE", "reviewer_name": "Dana Ops"}
    )
    assert first.status_code == 201
    second = await client.post(
        ctx["base"],
        json={"action": "REJECT", "reviewer_name": "Sam", "note": "changed my mind"},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DECISION_ALREADY_REVIEWED"
    assert await _status(client, ctx["document_id"]) == "APPROVED"


async def test_an_accepted_decision_cannot_be_reviewed(client: AsyncClient) -> None:
    ctx = await _accepted_decision(client)
    resp = await client.post(
        ctx["base"], json={"action": "REJECT", "reviewer_name": "Dana", "note": "no"}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DECISION_NOT_REVIEWABLE"
    assert await _status(client, ctx["document_id"]) == "COMPLETED"


async def test_unknown_links_are_404(client: AsyncClient) -> None:
    ctx = await _needs_review_decision(client)
    good = ctx["base"]
    body = {"action": "APPROVE", "reviewer_name": "Dana"}

    bad_decision = good.replace(ctx["decision_id"], str(uuid.uuid4()))
    r = await client.post(bad_decision, json=body)
    assert r.status_code == 404 and r.json()["error"]["code"] == "DECISION_NOT_FOUND"

    bad_doc = good.replace(ctx["document_id"], str(uuid.uuid4()))
    r = await client.post(bad_doc, json=body)
    assert r.status_code == 404 and r.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"

    bad_val = good.replace(ctx["validation_id"], str(uuid.uuid4()))
    r = await client.post(bad_val, json=body)
    assert r.status_code == 404 and r.json()["error"]["code"] == "VALIDATION_NOT_FOUND"


# --- retrieval -------------------------------------------------


async def test_get_decision_review_before_and_after(client: AsyncClient) -> None:
    ctx = await _needs_review_decision(client)
    before = await client.get(ctx["base"])
    assert before.status_code == 404
    assert before.json()["error"]["code"] == "REVIEW_NOT_FOUND"

    posted = await client.post(
        ctx["base"], json={"action": "APPROVE", "reviewer_name": "Dana Ops"}
    )
    review_id = posted.json()["review_id"]

    after = await client.get(ctx["base"])
    assert after.status_code == 200
    assert after.json()["review_id"] == review_id

    by_id = await client.get(f"/reviews/{review_id}")
    assert by_id.status_code == 200
    assert by_id.json()["document_id"] == ctx["document_id"]

    assert (await client.get(f"/reviews/{uuid.uuid4()}")).status_code == 404


# --- queue ---------------------------------------------------


async def test_queue_lists_pending_items_and_drops_resolved_ones(
    client: AsyncClient,
) -> None:
    ctx = await _needs_review_decision(client)

    queue = await client.get("/reviews/queue")
    assert queue.status_code == 200
    entries = queue.json()
    mine = [e for e in entries if e["document_id"] == ctx["document_id"]]
    assert len(mine) == 1
    entry = mine[0]
    assert entry["decision_id"] == ctx["decision_id"]
    assert entry["validation_id"] == ctx["validation_id"]
    assert entry["decision"]["outcome"] == "NEEDS_REVIEW"
    assert any(
        r["code"] == "manual_review_requested" for r in entry["decision"]["reasons"]
    )

    await client.post(
        ctx["base"], json={"action": "APPROVE", "reviewer_name": "Dana Ops"}
    )
    after = await client.get("/reviews/queue")
    assert all(e["document_id"] != ctx["document_id"] for e in after.json())


async def test_queue_is_fifo_by_decision_time(client: AsyncClient) -> None:
    first = await _needs_review_decision(client)
    second = await _needs_review_decision(client)

    order = [e["document_id"] for e in (await client.get("/reviews/queue")).json()]
    assert order.index(first["document_id"]) < order.index(second["document_id"])


# --- concurrency at the HTTP layer -------------------------


async def test_concurrent_http_submits_only_one_wins(client: AsyncClient) -> None:
    ctx = await _needs_review_decision(client)

    async def submit(name: str):
        return await client.post(
            ctx["base"], json={"action": "APPROVE", "reviewer_name": name}
        )

    a, b = await asyncio.gather(submit("Dana"), submit("Sam"))
    codes = sorted([a.status_code, b.status_code])
    assert codes == [201, 409]
    assert await _status(client, ctx["document_id"]) == "APPROVED"
