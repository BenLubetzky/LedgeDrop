"""Stage 4 verification suite (step 14).

One executable pass over the Stage 4 acceptance checklist in
``docs/stage-4-normalization.md``. Where a bullet already has exhaustive
coverage elsewhere this file exercises it *through the composed stack* instead
of re-deriving the matrix, so the engine, persistence, lifecycle, API, and
pipeline are shown to hold together. It also adds the hard "no AI, no network"
guards.

Checklist -> where it is proven
------------------------------
* Every supported date format ......... ``test_supported_date_formats`` (+
  ``test_normalization_normalizers.py::test_date_supported_formats``)
* Ambiguous / impossible dates ........ ``test_ambiguous_numeric_date_*`` /
  ``test_impossible_dates_*``
* Currencies (valid/invalid/missing/lower) ``test_currency_matrix``
* Decimal & thousands separators ...... ``test_number_matrix``
* Negative & malformed amounts ........ ``test_number_matrix``
* Quantity parsing .................... ``test_number_matrix``
* Text & identifier preservation ...... ``test_text_and_identifiers_preserved``
* Null & empty values ................. ``test_null_and_empty_are_null_without_errors``
* Line-item normalization ............. ``test_line_items_normalized_in_order``
* Structured error recording ......... ``test_structured_error_record`` /
  ``test_golden_invoice_with_field_errors``
* DB relationships & constraints ...... ``test_persisted_shape_and_cascade`` (+
  ``test_normalization_model.py``)
* Status transitions & retries ........ ``test_lifecycle_retry_and_history``
* Concurrent-processing protection .... ``test_concurrent_starts_only_one_wins``
* Transaction rollback ................ ``test_persist_failure_rolls_back``
* Retrieval & 404 / 409 .............. ``test_retrieval_404_and_409``
* Stage 3 raw values preserved ........ ``test_stage3_values_unchanged``
* No AI / external-network call ....... ``test_engine_makes_no_network_connection`` /
  ``test_deterministic_core_imports_no_ai_sdk``
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_extractor
from app.core.errors import ConflictError
from app.models import (
    Document,
    DocumentStatus,
    ExtractionStatus,
    NormalizationAttempt,
    NormalizationErrorCode,
    NormalizationFieldError,
    NormalizationLineItem,
    NormalizationStatus,
)
from app.schemas.extraction import InvoiceExtraction
from app.schemas.normalization import NormalizationErrorCode as EC
from app.services.processing.extraction import ExtractionService
from app.services.processing.normalization import (
    NormalizationRepository,
    NormalizationService,
    normalize_extraction,
)
from app.services.processing.normalization.normalizers import (
    normalize_money,
    normalize_quantity,
)

from tests._helpers import make_pdf

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_ENGINE_PATH = "app.services.processing.normalization.service.normalize_extraction"


# --- contract helpers --------------------------------------------------


def _pair(value=None, confidence=None) -> dict:
    return {"value": value, "confidence": confidence}


def _payload(**overrides) -> dict:
    """A schema-valid Stage 3 extraction payload ({value, confidence} envelopes).

    Money / quantity values must already be decimal-coercible - that is the
    Stage 3 contract - so number-*string* policy is checked against the
    normalizers directly in ``test_number_matrix``.
    """
    base = {
        "invoice_number": _pair("  INV 2026 / 0007 "),
        "invoice_date": _pair("15th January 2026."),
        "due_date": _pair("03/04/2026"),
        "vendor_name": _pair("  Café   Müller  & Co.  "),
        "vendor_tax_id": _pair("DE 123 456 789"),
        "customer_name": _pair("   "),
        "currency": _pair("usd"),
        "subtotal": _pair("-100.50"),
        "tax_amount": _pair(None),
        "total_amount": _pair("1234.56"),
        "line_items": [
            {
                "description": _pair("  Widget   A "),
                "quantity": _pair("2.000"),
                "unit_price": _pair("10.00"),
                "line_total": _pair("20.00"),
            }
        ],
    }
    base.update(overrides)
    return base


def _contract(**overrides) -> InvoiceExtraction:
    return InvoiceExtraction.model_validate(_payload(**overrides))


class _ScriptedProvider:
    """An extraction provider that returns a caller-supplied payload."""

    name = "scripted-verification"

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def extract(self, _prepared) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def _offline_extractor(app):
    app.dependency_overrides[get_extractor] = lambda: _ScriptedProvider(_payload())
    yield


def _use_provider(app, payload: dict) -> None:
    app.dependency_overrides[get_extractor] = lambda: _ScriptedProvider(payload)


async def _upload(client: AsyncClient) -> str:
    resp = await client.post(
        "/documents", files={"file": ("invoice.pdf", make_pdf(1), "application/pdf")}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["document_id"]


async def _make_document(session: AsyncSession) -> Document:
    doc = Document(
        original_filename="invoice.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="a" * 64,
        file_size_bytes=2048,
        page_count=1,
        status=DocumentStatus.UPLOADED,
    )
    session.add(doc)
    await session.flush()
    return doc


async def _completed_extraction(session: AsyncSession, **overrides):
    doc = await _make_document(session)
    contract = _contract(**overrides)

    async def produce(_doc):
        return contract

    return await ExtractionService(session).start(
        doc.document_id, produce=produce, provider_name="fake"
    )


# --- dates -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-15", "2026-01-15"),
        ("2026/1/5", "2026-01-05"),
        ("2026.01.15", "2026-01-15"),
        ("15 Jan 2026", "2026-01-15"),
        ("15 January 2026", "2026-01-15"),
        ("15-Jan-2026", "2026-01-15"),
        ("Jan 15, 2026", "2026-01-15"),
        ("January 15 2026", "2026-01-15"),
        ("1st Feb 2026", "2026-02-01"),
        ("15th January 2026.", "2026-01-15"),
        ("13/04/2026", "2026-04-13"),  # >12 rule picks the day
        ("04/13/2026", "2026-04-13"),
        ("29/02/2024", "2024-02-29"),  # real leap day
        ("2099-12-31", "2099-12-31"),  # no plausibility window
    ],
)
def test_supported_date_formats(raw: str, expected: str) -> None:
    normalized = normalize_extraction(_contract(invoice_date=_pair(raw)))
    assert normalized.invoice_date == expected
    assert normalized.errors == []


def test_ambiguous_numeric_date_is_day_first_and_not_an_error() -> None:
    normalized = normalize_extraction(_contract(due_date=_pair("03/04/2026")))
    assert normalized.due_date == "2026-04-03"  # DD/MM/YYYY default
    assert normalized.errors == []


@pytest.mark.parametrize(
    "raw",
    ["31/02/2026", "2026-02-30", "2026-13-01", "29/02/2026", "Feb 30, 2026",
     "13/13/2026", "15/01/26", "20260115", "Q1 2026", "15 Janvier 2026"],
)
def test_impossible_or_unrecognized_dates_become_invalid_date(raw: str) -> None:
    normalized = normalize_extraction(_contract(invoice_date=_pair(raw)))
    assert normalized.invoice_date is None
    assert [(e.field_path, e.code) for e in normalized.errors] == [
        ("invoice_date", EC.INVALID_DATE)
    ]
    assert normalized.errors[0].raw_value == raw


# --- currency ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_value", "expected_code"),
    [
        # The Stage 3 contract already guarantees null | 3 upper-case letters, so
        # the trim / blank cases are covered at the normalizer unit level
        # (test_normalization_normalizers.py); here we verify the values that can
        # actually reach normalization end to end.
        ("USD", "USD", None),
        ("usd", "USD", None),        # Stage 3 upper-cases; Stage 4 confirms, no-op
        (None, None, None),          # missing stays null, never defaulted
        ("XAU", None, EC.UNKNOWN_CURRENCY),   # precious metal, well-formed
        ("DEM", None, EC.UNKNOWN_CURRENCY),   # withdrawn
        ("ZZZ", None, EC.UNKNOWN_CURRENCY),
    ],
)
def test_currency_matrix(raw, expected_value, expected_code) -> None:
    normalized = normalize_extraction(_contract(currency=_pair(raw)))
    assert normalized.currency == expected_value
    codes = [(e.field_path, e.code) for e in normalized.errors]
    assert codes == ([] if expected_code is None else [("currency", expected_code)])


# --- numbers -----------------------------------------------------


def test_number_matrix() -> None:
    # Decimal passthrough via the engine: sign and scale preserved, never rounded.
    normalized = normalize_extraction(
        _contract(
            subtotal=_pair("-100.50"),
            tax_amount=_pair(None),
            total_amount=_pair("1234.5600"),
            line_items=[
                {
                    "description": _pair("x"),
                    "quantity": _pair("2.000"),
                    "unit_price": _pair("-3.5"),
                    "line_total": _pair("7.00"),
                }
            ],
        )
    )
    assert str(normalized.subtotal) == "-100.50"
    assert normalized.tax_amount is None
    assert str(normalized.total_amount) == "1234.5600"
    assert str(normalized.line_items[0].quantity) == "2.000"
    assert normalized.line_items[0].unit_price == Decimal("-3.5")
    assert normalized.errors == []

    # String policy (future providers) - checked against the normalizers directly.
    for money in ("1,234.56", "1 234.56", "1 234 567.89", "$1,234.56", "(123.45)", "123.45-"):
        assert normalize_money(money).error is None
    assert normalize_money("1,234.56").value == Decimal("1234.56")
    assert normalize_money("1.234,56").error.code is EC.AMBIGUOUS_NUMBER   # decimal comma
    for bad in ("1,23,456", "12.34.56", "1e5", ".5", "abc"):
        assert normalize_money(bad).error.code is EC.INVALID_NUMBER
    assert normalize_quantity("1 234.5").value == Decimal("1234.5")
    assert normalize_quantity("1.234,56").error.code is EC.AMBIGUOUS_NUMBER


# --- text & identifiers -----------------------------------------


def test_text_and_identifiers_preserved() -> None:
    normalized = normalize_extraction(
        _contract(
            invoice_number=_pair("  INV 2026 / 0007 "),
            vendor_name=_pair("  Café   Müller  & Co.  "),
            vendor_tax_id=_pair("DE  123   456 789"),
            customer_name=_pair("Acme, Inc. (DE) — Süd/West & Partner"),
        )
    )
    assert normalized.invoice_number == "INV 2026 / 0007"   # separators kept, not parsed
    assert normalized.vendor_name == "Café Müller & Co."    # collapsed, accents/case kept
    assert normalized.vendor_tax_id == "DE 123 456 789"
    assert normalized.customer_name == "Acme, Inc. (DE) — Süd/West & Partner"
    assert normalized.errors == []

    over = normalize_extraction(_contract(vendor_name=_pair("x" * 300)))
    assert over.vendor_name is None
    assert [(e.field_path, e.code) for e in over.errors] == [("vendor_name", EC.TEXT_TOO_LONG)]
    assert len(over.errors[0].raw_value) == 300   # never silently truncated


# --- null / empty -------------------------------------------


def test_null_and_empty_are_null_without_errors() -> None:
    empty = {name: _pair(None) for name in (
        "invoice_number", "invoice_date", "due_date", "vendor_name",
        "vendor_tax_id", "customer_name", "currency", "subtotal",
        "tax_amount", "total_amount",
    )}
    empty["vendor_name"] = _pair("   ")
    empty["invoice_number"] = _pair("")
    empty["line_items"] = []

    normalized = normalize_extraction(InvoiceExtraction.model_validate(empty))

    for name in empty:
        if name != "line_items":
            assert getattr(normalized, name) is None
    assert normalized.errors == []


# --- line items -------------------------------------------


def test_line_items_normalized_in_order() -> None:
    normalized = normalize_extraction(
        _contract(
            line_items=[
                {"description": _pair(" First "), "quantity": _pair("2"),
                 "unit_price": _pair("10.00"), "line_total": _pair("20.00")},
                {"description": _pair("d" * 600), "quantity": _pair("1"),
                 "unit_price": _pair("5.00"), "line_total": _pair("5.00")},
                {"description": _pair("Third"), "quantity": _pair(None),
                 "unit_price": _pair(None), "line_total": _pair(None)},
            ]
        )
    )
    assert [li.description for li in normalized.line_items] == ["First", None, "Third"]
    assert normalized.line_items[0].quantity == Decimal("2")
    assert normalized.line_items[2].quantity is None
    assert [(e.field_path, e.code) for e in normalized.errors] == [
        ("line_items.1.description", EC.TEXT_TOO_LONG)
    ]


# --- structured error record ------------------------------


def test_structured_error_record() -> None:
    normalized = normalize_extraction(_contract(total_amount=_pair(None), invoice_date=_pair("31/02/2026")))
    err = next(e for e in normalized.errors if e.field_path == "invoice_date")
    assert err.raw_value == "31/02/2026"          # source value, stringified
    assert err.code is EC.INVALID_DATE            # closed set
    assert err.message and "31/02/2026" not in err.message   # generic, client-safe
    assert normalized.invoice_date is None        # errored field is null


# --- golden end-to-end runs through the pipeline ---------


async def test_golden_clean_invoice_end_to_end(client: AsyncClient, app) -> None:
    _use_provider(app, _payload())   # the default spread: all values normalize cleanly
    document_id = await _upload(client)

    resp = await client.post(f"/documents/{document_id}/pipeline")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["extraction"]["status"] == "COMPLETED"
    assert body["normalization"]["status"] == "COMPLETED"
    data = body["normalization"]["data"]
    assert data["invoice_number"] == "INV 2026 / 0007"
    assert data["invoice_date"] == "2026-01-15"
    assert data["due_date"] == "2026-04-03"          # day-first default
    assert data["vendor_name"] == "Café Müller & Co."
    assert data["vendor_tax_id"] == "DE 123 456 789"
    assert data["customer_name"] is None             # whitespace-only -> null
    assert data["currency"] == "USD"
    assert data["subtotal"] == "-100.50"            # negative + scale preserved
    assert data["tax_amount"] is None
    assert data["total_amount"] == "1234.56"
    assert data["line_items"] == [
        {"description": "Widget A", "quantity": "2.000",
         "unit_price": "10.00", "line_total": "20.00"}
    ]
    assert data["errors"] == []


async def test_golden_invoice_with_field_errors_end_to_end(
    client: AsyncClient, app
) -> None:
    _use_provider(
        app,
        _payload(
            invoice_date=_pair("31/02/2026"),
            currency=_pair("XAU"),
            vendor_name=_pair("x" * 300),
            line_items=[
                {"description": _pair("d" * 600), "quantity": _pair("2"),
                 "unit_price": _pair("10.00"), "line_total": _pair("20.00")},
            ],
        ),
    )
    document_id = await _upload(client)

    body = (await client.post(f"/documents/{document_id}/pipeline")).json()

    assert body["normalization"]["status"] == "COMPLETED"   # field errors are not a failure
    data = body["normalization"]["data"]
    assert data["invoice_date"] is None
    assert data["currency"] is None
    assert data["vendor_name"] is None
    assert data["line_items"][0]["description"] is None
    assert {(e["field_path"], e["code"]) for e in data["errors"]} == {
        ("invoice_date", "invalid_date"),
        ("currency", "unknown_currency"),
        ("vendor_name", "text_too_long"),
        ("line_items.0.description", "text_too_long"),
    }
    # untouched siblings still normalized
    assert data["invoice_number"] == "INV 2026 / 0007"
    assert data["line_items"][0]["unit_price"] == "10.00"
    # Stage 3 view still shows the raw, unmodified values
    assert body["extraction"]["data"]["invoice_date"]["value"] == "31/02/2026"
    assert body["extraction"]["data"]["currency"]["value"] == "XAU"


# --- Stage 3 preservation ------------------------------


async def test_stage3_values_unchanged(client: AsyncClient, app) -> None:
    _use_provider(app, _payload(invoice_date=_pair("31/02/2026")))
    document_id = await _upload(client)

    piped = (await client.post(f"/documents/{document_id}/pipeline")).json()
    extraction_id = piped["extraction"]["extraction_id"]

    after = await client.get(f"/documents/{document_id}/extractions/{extraction_id}")
    assert after.status_code == 200
    # the extraction record the pipeline returned and a fresh read are identical
    assert after.json() == piped["extraction"]

    doc = (await client.get(f"/documents/{document_id}")).json()
    assert doc["status"] == "COMPLETED"   # normalization never moves the document


# --- lifecycle, retries, history --------------------


async def test_lifecycle_retry_and_history(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    extraction_id = (await _completed_extraction(db_session)).extraction_id
    svc = NormalizationService(db_session)

    monkeypatch.setattr(_ENGINE_PATH, lambda _c: (_ for _ in ()).throw(RuntimeError("crash")))
    first = await svc.start(extraction_id)
    assert first.status is NormalizationStatus.FAILED
    assert first.failure_code == "NORMALIZATION_FAILED"

    monkeypatch.undo()
    second = await svc.retry(extraction_id)
    assert second.status is NormalizationStatus.COMPLETED
    assert second.attempt_number == 2

    history = await NormalizationRepository(db_session).list_for_extraction(extraction_id)
    assert [(a.attempt_number, a.status) for a in history] == [
        (1, NormalizationStatus.FAILED),
        (2, NormalizationStatus.COMPLETED),
    ]
    assert history[0].invoice_number is None   # the failed attempt holds no result


# --- concurrency ----------------------------------


async def test_concurrent_starts_only_one_wins(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup:
        extraction_id = (await _completed_extraction(setup)).extraction_id

    async def run():
        async with session_factory() as session:
            return await NormalizationService(session).start(extraction_id)

    results = await asyncio.gather(run(), run(), return_exceptions=True)
    winners = [r for r in results if isinstance(r, NormalizationAttempt)]
    conflicts = [r for r in results if isinstance(r, ConflictError)]
    assert len(winners) == 1 and len(conflicts) == 1
    assert winners[0].status is NormalizationStatus.COMPLETED

    async with session_factory() as check:
        history = await NormalizationRepository(check).list_for_extraction(extraction_id)
    assert [a.attempt_number for a in history] == [1]


# --- transaction rollback ------------------------


async def test_persist_failure_rolls_back(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    extraction_id = (await _completed_extraction(db_session)).extraction_id

    def explode(self, attempt, normalized):
        attempt.errors = [
            NormalizationFieldError(
                field_path="currency", raw_value="ZZZ",
                code=NormalizationErrorCode.UNKNOWN_CURRENCY, message="x",
            )
        ]
        raise RuntimeError("write failed mid-flush")

    monkeypatch.setattr(NormalizationRepository, "apply_result", explode)

    attempt = await NormalizationService(db_session).start(extraction_id)
    assert attempt.status is NormalizationStatus.FAILED
    assert list(attempt.line_items) == [] and list(attempt.errors) == []

    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(NormalizationLineItem)) == 0
    assert await db_session.scalar(select(func.count()).select_from(NormalizationFieldError)) == 0


# --- persisted shape + cascade ----------------


async def test_persisted_shape_and_cascade(db_session: AsyncSession) -> None:
    extraction = await _completed_extraction(
        db_session,
        line_items=[
            {"description": _pair("A"), "quantity": _pair("1"),
             "unit_price": _pair("1.00"), "line_total": _pair("1.00")},
            {"description": _pair("B"), "quantity": _pair("2"),
             "unit_price": _pair("2.00"), "line_total": _pair("4.00")},
        ],
        currency=_pair("XAU"),  # -> one field error row
    )
    document_id = extraction.document_id
    attempt = await NormalizationService(db_session).start(extraction.extraction_id)

    assert [li.position for li in attempt.line_items] == [0, 1]
    assert [e.field_path for e in attempt.errors] == ["currency"]
    assert attempt.source_extraction.extraction_id == extraction.extraction_id

    doc = await db_session.get(Document, document_id)
    await db_session.delete(doc)
    await db_session.commit()
    assert await db_session.scalar(select(func.count()).select_from(NormalizationAttempt)) == 0
    assert await db_session.scalar(select(func.count()).select_from(NormalizationLineItem)) == 0
    assert await db_session.scalar(select(func.count()).select_from(NormalizationFieldError)) == 0


# --- retrieval + 404 / 409 --------------------


async def test_retrieval_404_and_409(client: AsyncClient, app) -> None:
    _use_provider(app, _payload())
    document_id = await _upload(client)
    piped = (await client.post(f"/documents/{document_id}/pipeline")).json()
    extraction_id = piped["extraction"]["extraction_id"]
    normalization_id = piped["normalization"]["normalization_id"]
    base = f"/documents/{document_id}/extractions/{extraction_id}/normalizations"

    assert (await client.get(f"{base}/latest")).json()["normalization_id"] == normalization_id
    assert (await client.get(f"{base}/{normalization_id}")).status_code == 200
    assert [r["attempt_number"] for r in (await client.get(base)).json()] == [1]

    assert (await client.get(f"{base}/{uuid.uuid4()}")).json()["error"]["code"] == "NORMALIZATION_NOT_FOUND"
    bad = f"/documents/{document_id}/extractions/{uuid.uuid4()}/normalizations/latest"
    assert (await client.get(bad)).json()["error"]["code"] == "EXTRACTION_NOT_FOUND"
    miss = f"/documents/{uuid.uuid4()}/extractions/{uuid.uuid4()}/normalizations"
    assert (await client.get(miss)).json()["error"]["code"] == "DOCUMENT_NOT_FOUND"

    # already normalized -> 409 on an explicit second start
    conflict = await client.post(base)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "EXTRACTION_ALREADY_NORMALIZED"


# --- no AI, no network ----------------------


def test_engine_makes_no_network_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def _forbidden(*_a, **_k):
        raise AssertionError("normalization attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)

    # A spread that drives every normalizer branch, including the error paths.
    normalized = normalize_extraction(
        _contract(
            invoice_date=_pair("31/02/2026"),
            due_date=_pair("2026-01-15"),
            currency=_pair("XAU"),
            vendor_name=_pair("x" * 300),
        )
    )
    assert normalized.invoice_date is None and normalized.due_date == "2026-01-15"
    assert len(normalized.errors) == 3


_NETWORK_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+(openai|anthropic|httpx|requests|urllib|http\.client|aiohttp|socket)\b",
    re.MULTILINE,
)


def test_normalization_source_makes_no_ai_or_network_import() -> None:
    """No file under the normalization package imports an AI or network client.

    The deterministic core carries no such dependency in its own source. (The
    lifecycle service reuses ``ExtractionRepository`` for data access only - a
    pure DB reader with no network of its own.)
    """
    pkg = _BACKEND_DIR / "app" / "services" / "processing" / "normalization"
    offenders = {
        path.relative_to(_BACKEND_DIR).as_posix(): _NETWORK_IMPORT_RE.findall(
            path.read_text(encoding="utf-8")
        )
        for path in pkg.rglob("*.py")
        if _NETWORK_IMPORT_RE.search(path.read_text(encoding="utf-8"))
    }
    assert offenders == {}, offenders


def test_normalization_schema_layer_imports_no_ai_sdk() -> None:
    """Importing only the Stage 4 schema/contract layer pulls in no AI SDK."""
    probe = (
        "import sys; "
        "import app.schemas.normalization; "
        "import app.schemas.normalization_persistence; "
        "import app.schemas.normalization_api; "
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
        f"AI SDK imported by the Stage 4 schema layer: {result.stdout.strip()}\n{result.stderr}"
    )
