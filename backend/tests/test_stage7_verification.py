"""Stage 7 verification suite (package 5).

One executable pass over the Stage 7 acceptance checklist in
``docs/stage-7-review.md``. Where a bullet already has dense coverage in a
per-package file (the contract, persistence, model, repository, migration,
lifecycle, service, and API each have their own from packages 1-2) this file
exercises it *through the composed stack* - upload -> extraction ->
normalization -> validation -> decision -> review, over real HTTP - so the
service, persistence, and API are shown to hold together. It also adds the
full-chain immutability proof and the "no AI / no network" guard for the
review subsystem.

Checklist -> where it is proven
--------------------------------
* Authorization / ownership: only a COMPLETED ``NEEDS_REVIEW`` decision is
  reviewable; a Stage 6 ``ACCEPTED`` result cannot be overridden; each broken
  chain link returns its own 404
  ``test_an_accepted_decision_cannot_be_reviewed`` /
  ``test_submit_404s_per_broken_chain_link`` (+ ``test_review_lifecycle.py``,
  ``test_reviews_api.py``)
* Queue behaviour: a ``NEEDS_REVIEW`` invoice appears with its reasons; an
  ``ACCEPTED`` one never does; FIFO order; resolved items drop out
  ``test_needs_review_invoice_is_queued_with_its_reasons`` /
  ``test_accepted_invoice_is_never_queued`` /
  ``test_queue_is_fifo_and_drops_resolved_items`` (+
  ``test_review_repository.py``, ``test_reviews_api.py``)
* Approval / rejection: ``APPROVE`` -> ``documents.status = APPROVED``;
  ``REJECT`` (+ note) -> ``REJECTED``; a note is required to reject
  ``test_approve_moves_the_document_end_to_end`` /
  ``test_reject_with_note_moves_the_document_end_to_end`` /
  ``test_reject_without_a_note_is_422_and_changes_nothing``
* Duplicate submissions: the second (or concurrent) submit -> ``409``, first
  outcome kept, one review row
  ``test_second_submit_is_409_and_keeps_the_first_outcome`` /
  ``test_concurrent_submits_only_one_wins``
* Concurrent reviewers ................ ``test_concurrent_submits_only_one_wins``
  (+ ``test_review_service.py::test_two_concurrent_submits_only_one_wins``)
* Stale decisions: a superseded chain -> ``409 STALE_DECISION_SOURCE``, nothing
  written .............. ``test_a_superseded_chain_is_rejected_as_stale`` (+
  ``test_review_service.py::test_stale_decision_source_is_rejected``)
* Audit history: the recorded review is retrievable by id and decision-scoped,
  carries who / what / when, and is stable across re-reads and the document's
  own status change ......... ``test_recorded_review_is_retrievable_and_stable``
* Status transitions: ``NEEDS_REVIEW -> APPROVED | REJECTED`` only; every
  Stage 2-6 row and the stored PDF unchanged; the ``documents`` row changes in
  exactly ``status`` + ``updated_at``
  ``test_reviewing_changes_only_the_document_status`` (+
  ``test_review_service.py::test_source_rows_are_untouched_apart_from_document_status``)
* Migration round trip: ``0006_review_tables`` upgrade -> downgrade -> upgrade
  on real PostgreSQL, Stage 2-6 rows byte-for-byte, ``document_status`` loses /
  regains ``APPROVED`` / ``REJECTED``, ``alembic check`` clean
  ``test_review_migration.py`` (dedicated) + Stage 6's own
  ``test_stage6_verification.py::test_migration_upgrade_downgrade_preserves_stage2_5_data``
* Preservation of all Stage 2-6 data ...... ``test_reviewing_changes_only_the_document_status``
  + the migration test above
* DB relationships & constraints ...... ``test_review_model.py``
* No AI / external-network call ........ ``test_review_subsystem_source_has_no_ai_or_network_import`` /
  ``test_review_schema_layer_imports_no_ai_sdk``
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.api.deps import get_extractor
from app.models import ExtractionAttempt, ExtractionStatus

from tests._helpers import make_pdf

_BACKEND_DIR = Path(__file__).resolve().parents[1]

# Every Stage 2-6 table. A review must leave all of these byte-for-byte
# unchanged except for the one authorised write to documents.status.
_STAGE2_6_TABLES = (
    "documents",
    "invoice_extractions",
    "invoice_line_items",
    "invoice_normalizations",
    "invoice_normalized_line_items",
    "invoice_normalization_errors",
    "invoice_validations",
    "invoice_validation_findings",
    "invoice_decisions",
    "invoice_decision_reasons",
)


def _pair(value=None, confidence="0.95") -> dict:
    return {"value": value, "confidence": confidence}


def _clean_payload() -> dict:
    """A schema-valid extraction payload that reconciles cleanly on every rule."""
    return {
        "invoice_number": _pair("INV-2026-001"),
        "invoice_date": _pair("2026-03-01"),
        "due_date": _pair("2026-03-15"),
        "vendor_name": _pair("Acme GmbH"),
        "vendor_tax_id": _pair("DE123456789"),
        "customer_name": _pair("Beta Ltd"),
        "currency": _pair("EUR"),
        "subtotal": _pair("100.00"),
        "tax_amount": _pair("19.00"),
        "total_amount": _pair("119.00"),
        "line_items": [
            {
                "description": _pair("Widget"),
                "quantity": _pair("2"),
                "unit_price": _pair("50.00"),
                "line_total": _pair("100.00"),
            }
        ],
    }


class _ScriptedProvider:
    name = "scripted-stage7"

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def extract(self, _prepared) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def _offline_extractor(app):
    app.dependency_overrides[get_extractor] = lambda: _ScriptedProvider(_clean_payload())
    yield


async def _upload(client: AsyncClient) -> str:
    resp = await client.post(
        "/documents", files={"file": ("invoice.pdf", make_pdf(1), "application/pdf")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["document_id"]


async def _pipeline_to_needs_review(
    client: AsyncClient, *, manual_review: bool = True
) -> dict:
    """Upload + run the whole pipeline; return the ``PipelineRunResult`` JSON.

    With ``manual_review=True`` the clean fake invoice still comes out
    ``NEEDS_REVIEW`` and the owning document moves to ``NEEDS_REVIEW``.
    """
    document_id = await _upload(client)
    resp = await client.post(
        f"/documents/{document_id}/pipeline",
        json={"manual_review_requested": manual_review} if manual_review else None,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    expected = "NEEDS_REVIEW" if manual_review else "ACCEPTED"
    assert body["decision"]["status"] == "COMPLETED"
    assert body["decision"]["outcome"] == expected
    return body


def _review_base(pipe: dict) -> str:
    e, n = pipe["extraction"], pipe["normalization"]
    v, d = pipe["validation"], pipe["decision"]
    return (
        f"/documents/{e['document_id']}/extractions/{e['extraction_id']}"
        f"/normalizations/{n['normalization_id']}/validations/{v['validation_id']}"
        f"/decisions/{d['decision_id']}/review"
    )


async def _doc_status(client: AsyncClient, pipe: dict) -> str:
    resp = await client.get(f"/documents/{pipe['extraction']['document_id']}")
    assert resp.status_code == 200
    return resp.json()["status"]


async def _snapshot(
    conn: AsyncConnection | AsyncSession, tables: tuple[str, ...] = _STAGE2_6_TABLES
) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for table in tables:
        result = await conn.execute(sa.text(f'SELECT * FROM "{table}" ORDER BY 1'))
        out[table] = [dict(row) for row in result.mappings().all()]
    return out


# --- authorization / ownership -------------------------------------


async def test_an_accepted_decision_cannot_be_reviewed(client: AsyncClient) -> None:
    pipe = await _pipeline_to_needs_review(client, manual_review=False)
    base = _review_base(pipe)

    resp = await client.post(
        base, json={"action": "REJECT", "reviewer_name": "Dana", "note": "no"}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DECISION_NOT_REVIEWABLE"
    assert await _doc_status(client, pipe) == "COMPLETED"
    assert (await client.get(base)).status_code == 404  # no review recorded


async def test_submit_404s_per_broken_chain_link(client: AsyncClient) -> None:
    pipe = await _pipeline_to_needs_review(client)
    base = _review_base(pipe)
    body = {"action": "APPROVE", "reviewer_name": "Dana"}
    e, n = pipe["extraction"], pipe["normalization"]
    v, d = pipe["validation"], pipe["decision"]

    for real_id, code in [
        (e["document_id"], "DOCUMENT_NOT_FOUND"),
        (e["extraction_id"], "EXTRACTION_NOT_FOUND"),
        (n["normalization_id"], "NORMALIZATION_NOT_FOUND"),
        (v["validation_id"], "VALIDATION_NOT_FOUND"),
        (d["decision_id"], "DECISION_NOT_FOUND"),
    ]:
        broken = base.replace(real_id, str(uuid.uuid4()))
        resp = await client.post(broken, json=body)
        assert resp.status_code == 404, (code, resp.text)
        assert resp.json()["error"]["code"] == code
    assert await _doc_status(client, pipe) == "NEEDS_REVIEW"


# --- queue ---------------------------------------------------------


async def test_needs_review_invoice_is_queued_with_its_reasons(
    client: AsyncClient,
) -> None:
    pipe = await _pipeline_to_needs_review(client)
    entries = (await client.get("/reviews/queue")).json()

    mine = [e for e in entries if e["document_id"] == pipe["extraction"]["document_id"]]
    assert len(mine) == 1
    entry = mine[0]
    assert entry["decision_id"] == pipe["decision"]["decision_id"]
    assert entry["validation_id"] == pipe["validation"]["validation_id"]
    assert entry["decision"]["outcome"] == "NEEDS_REVIEW"
    assert any(
        r["code"] == "manual_review_requested" and r["triggers_review"]
        for r in entry["decision"]["reasons"]
    )
    # the queue entry exposes no internal diagnostics
    assert "policy_version" not in entry
    assert "context" not in str(entry)


async def test_accepted_invoice_is_never_queued(client: AsyncClient) -> None:
    pipe = await _pipeline_to_needs_review(client, manual_review=False)
    entries = (await client.get("/reviews/queue")).json()
    assert all(
        e["document_id"] != pipe["extraction"]["document_id"] for e in entries
    )


async def test_queue_is_fifo_and_drops_resolved_items(client: AsyncClient) -> None:
    first = await _pipeline_to_needs_review(client)
    second = await _pipeline_to_needs_review(client)

    order = [e["document_id"] for e in (await client.get("/reviews/queue")).json()]
    first_doc = first["extraction"]["document_id"]
    second_doc = second["extraction"]["document_id"]
    assert order.index(first_doc) < order.index(second_doc)

    resp = await client.post(
        _review_base(first), json={"action": "APPROVE", "reviewer_name": "Dana"}
    )
    assert resp.status_code == 201

    after = [e["document_id"] for e in (await client.get("/reviews/queue")).json()]
    assert first_doc not in after
    assert second_doc in after


# --- approve / reject --------------------------------------------


async def test_approve_moves_the_document_end_to_end(client: AsyncClient) -> None:
    pipe = await _pipeline_to_needs_review(client)
    resp = await client.post(
        _review_base(pipe),
        json={"action": "APPROVE", "reviewer_name": "  Dana Ops  "},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["action"] == "APPROVE"
    assert body["reviewer_name"] == "Dana Ops"  # trimmed
    assert body["note"] is None
    assert body["document_id"] == pipe["extraction"]["document_id"]
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
    assert await _doc_status(client, pipe) == "APPROVED"


async def test_reject_with_note_moves_the_document_end_to_end(
    client: AsyncClient,
) -> None:
    pipe = await _pipeline_to_needs_review(client)
    resp = await client.post(
        _review_base(pipe),
        json={"action": "REJECT", "reviewer_name": "Dana", "note": "duplicate of INV-1"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["note"] == "duplicate of INV-1"
    assert await _doc_status(client, pipe) == "REJECTED"


async def test_reject_without_a_note_is_422_and_changes_nothing(
    client: AsyncClient,
) -> None:
    pipe = await _pipeline_to_needs_review(client)
    resp = await client.post(
        _review_base(pipe), json={"action": "REJECT", "reviewer_name": "Dana"}
    )
    assert resp.status_code == 422
    assert await _doc_status(client, pipe) == "NEEDS_REVIEW"
    assert (await client.get(_review_base(pipe))).status_code == 404


# --- duplicate / concurrent ------------------------------------


async def test_second_submit_is_409_and_keeps_the_first_outcome(
    client: AsyncClient,
) -> None:
    pipe = await _pipeline_to_needs_review(client)
    base = _review_base(pipe)

    first = await client.post(base, json={"action": "APPROVE", "reviewer_name": "Dana"})
    assert first.status_code == 201
    review_id = first.json()["review_id"]

    second = await client.post(
        base, json={"action": "REJECT", "reviewer_name": "Sam", "note": "changed my mind"}
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DECISION_ALREADY_REVIEWED"

    assert await _doc_status(client, pipe) == "APPROVED"
    # the recorded review is still the first one, unchanged
    assert (await client.get(base)).json()["review_id"] == review_id
    assert (await client.get(base)).json()["action"] == "APPROVE"


async def test_concurrent_submits_only_one_wins(client: AsyncClient) -> None:
    pipe = await _pipeline_to_needs_review(client)
    base = _review_base(pipe)

    a, b = await asyncio.gather(
        client.post(base, json={"action": "APPROVE", "reviewer_name": "Dana"}),
        client.post(base, json={"action": "APPROVE", "reviewer_name": "Sam"}),
    )
    assert sorted([a.status_code, b.status_code]) == [201, 409]
    losing = next(r for r in (a, b) if r.status_code == 409)
    assert losing.json()["error"]["code"] == "DECISION_ALREADY_REVIEWED"
    assert await _doc_status(client, pipe) == "APPROVED"


# --- stale decisions -----------------------------------------


async def test_a_superseded_chain_is_rejected_as_stale(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    pipe = await _pipeline_to_needs_review(client)
    base = _review_base(pipe)
    document_id = pipe["extraction"]["document_id"]

    # Manufacture a newer COMPLETED extraction for the same document, directly
    # against the database - not reachable through today's API, but the exact
    # shape a future re-extraction feature would produce and what this guard
    # exists to catch.
    await db_session.rollback()
    db_session.add(
        ExtractionAttempt(
            document_id=uuid.UUID(document_id),
            attempt_number=2,
            status=ExtractionStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            provider_name="fake",
        )
    )
    await db_session.commit()

    resp = await client.post(base, json={"action": "APPROVE", "reviewer_name": "Dana"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "STALE_DECISION_SOURCE"
    assert (await client.get(base)).status_code == 404  # nothing written
    assert await _doc_status(client, pipe) == "NEEDS_REVIEW"


# --- audit history -----------------------------------------


async def test_recorded_review_is_retrievable_and_stable(client: AsyncClient) -> None:
    pipe = await _pipeline_to_needs_review(client)
    base = _review_base(pipe)

    posted = await client.post(
        base, json={"action": "REJECT", "reviewer_name": "Dana Ops", "note": "wrong total"}
    )
    assert posted.status_code == 201
    review_id = posted.json()["review_id"]

    by_id = (await client.get(f"/reviews/{review_id}")).json()
    scoped = (await client.get(base)).json()
    assert by_id == scoped  # same record, both routes
    assert by_id["action"] == "REJECT"
    assert by_id["reviewer_name"] == "Dana Ops"  # who
    assert by_id["note"] == "wrong total"  # what
    assert by_id["reviewed_at"] and by_id["created_at"]  # when
    assert by_id["document_id"] == pipe["extraction"]["document_id"]

    # the document is now terminal, but the historical read is preserved and
    # byte-identical on a re-fetch
    assert await _doc_status(client, pipe) == "REJECTED"
    assert (await client.get(f"/reviews/{review_id}")).json() == by_id
    assert (await client.get(f"/reviews/{uuid.uuid4()}")).status_code == 404


# --- status transitions + full-chain immutability ----------


@pytest.mark.parametrize(
    ("action", "note", "final_status"),
    [
        ("APPROVE", None, "APPROVED"),
        ("REJECT", "not our vendor", "REJECTED"),
    ],
)
async def test_reviewing_changes_only_the_document_status(
    client: AsyncClient,
    db_session: AsyncSession,
    action: str,
    note: str | None,
    final_status: str,
) -> None:
    pipe = await _pipeline_to_needs_review(client)
    base = _review_base(pipe)
    document_id = pipe["extraction"]["document_id"]

    await db_session.rollback()
    before = await _snapshot(db_session)
    before_pdf = (await client.get(f"/documents/{document_id}/file")).content

    payload: dict = {"action": action, "reviewer_name": "Dana"}
    if note is not None:
        payload["note"] = note
    assert (await client.post(base, json=payload)).status_code == 201

    await db_session.rollback()
    after = await _snapshot(db_session)
    after_pdf = (await client.get(f"/documents/{document_id}/file")).content

    for table in _STAGE2_6_TABLES:
        if table == "documents":
            continue
        assert after[table] == before[table], f"{table} changed"

    (before_doc,) = before["documents"]
    (after_doc,) = after["documents"]
    changed = {k for k in before_doc if before_doc[k] != after_doc[k]}
    assert changed == {"status", "updated_at"}
    assert before_doc["status"] == "NEEDS_REVIEW"
    assert after_doc["status"] == final_status
    assert after_pdf == before_pdf


# --- no AI, no network -------------------------------------


_NETWORK_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+"
    r"(openai|anthropic|httpx|requests|urllib|http\.client|aiohttp|socket)\b",
    re.MULTILINE,
)


def test_review_subsystem_source_has_no_ai_or_network_import() -> None:
    roots = (
        _BACKEND_DIR / "app" / "services" / "processing" / "review",
        _BACKEND_DIR / "app" / "schemas" / "review.py",
        _BACKEND_DIR / "app" / "schemas" / "review_persistence.py",
        _BACKEND_DIR / "app" / "schemas" / "review_api.py",
        _BACKEND_DIR / "app" / "models" / "review.py",
        _BACKEND_DIR / "app" / "api" / "reviews.py",
    )
    files: list[Path] = []
    for root in roots:
        files.extend(root.rglob("*.py") if root.is_dir() else [root])

    offenders = {
        path.relative_to(_BACKEND_DIR).as_posix(): _NETWORK_IMPORT_RE.findall(
            path.read_text(encoding="utf-8")
        )
        for path in files
        if _NETWORK_IMPORT_RE.search(path.read_text(encoding="utf-8"))
    }
    assert offenders == {}, offenders


def test_review_schema_layer_imports_no_ai_sdk() -> None:
    """Importing the Stage 7 schema/contract layer pulls in no AI SDK.

    The service is not imported here by dotted path because the ``review``
    package ``__init__`` binds the service, which reuses the Stage 3-6
    repositories (pure DB readers) whose package transitively imports the real
    provider SDK - the same documented caveat Stage 4-6's equivalent tests
    carry.
    """
    probe = (
        "import sys; "
        "import app.schemas.review; "
        "import app.schemas.review_persistence; "
        "import app.schemas.review_api; "
        "import app.models.review; "
        "leaked = sorted(m for m in ('openai', 'anthropic') if m in sys.modules); "
        "print(','.join(leaked)); "
        "sys.exit(1 if leaked else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(_BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"AI SDK imported by the Stage 7 schema layer: "
        f"{result.stdout.strip()}\n{result.stderr}"
    )
