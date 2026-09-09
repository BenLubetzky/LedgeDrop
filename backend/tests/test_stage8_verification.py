"""Composed Stage 8 acceptance checks that cross package boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path

from httpx import AsyncClient

from tests.test_corrections_api import _full_correction, _pipeline_document

_CORRECTION_DIR = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "services"
    / "processing"
    / "correction"
)


async def test_concurrent_submissions_keep_one_outcome(
    client: AsyncClient,
) -> None:
    document_id, pipeline = await _pipeline_document(client, manual_review=True)
    decision_id = pipeline["decision"]["decision_id"]

    async def submit(reviewer: str):
        return await client.post(
            f"/documents/{document_id}/corrections",
            json=_full_correction(decision_id, reviewer_name=reviewer),
        )

    first, second = await asyncio.gather(submit("Dana Ops"), submit("Sam Ops"))
    assert sorted((first.status_code, second.status_code)) == [201, 409]

    history = await client.get(f"/documents/{document_id}/corrections")
    assert history.status_code == 200
    assert len(history.json()) == 1
    assert history.json()[0]["status"] == "COMPLETED"


def test_correction_subsystem_has_no_ai_or_network_import() -> None:
    forbidden = ("import openai", "from openai", "import httpx", "import requests")
    for source_file in _CORRECTION_DIR.glob("*.py"):
        source = source_file.read_text(encoding="utf-8").lower()
        assert not any(token in source for token in forbidden), source_file

