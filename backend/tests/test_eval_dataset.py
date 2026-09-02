"""Integrity tests for the extraction evaluation dataset (Stage 3, step 10).

These do not measure extraction accuracy (that is step 11). They check the
dataset is well-formed: the manifest parses, the files exist and behave as
declared, the ground truth fits the invoice contract, and the committed PDFs
match a fresh deterministic generation.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from pypdf import PdfReader

from app.schemas.extraction import InvoiceExtraction
from evaluation.dataset import CRITICAL_FIELDS, REQUIRED_CATEGORIES, load_cases
from evaluation.generate_invoices import build_all

CASES = load_cases()
CASES_BY_ID = {c.id: c for c in CASES}


def test_manifest_is_non_trivial_and_files_exist() -> None:
    assert len(CASES) >= 8
    for case in CASES:
        assert case.path.is_file(), case.path
        assert case.path.stat().st_size > 0
        assert case.text_layer in {"DIGITAL", "ABSENT", "UNREADABLE"}


def test_every_required_category_is_represented() -> None:
    assert {c.category for c in CASES} >= REQUIRED_CATEGORIES


def test_exactly_one_readable_non_invoice() -> None:
    non_invoices = [c for c in CASES if not c.is_invoice]
    assert [c.id for c in non_invoices] == ["not_an_invoice"]
    assert non_invoices[0].readable is True
    assert non_invoices[0].expected is None


def test_cases_without_ground_truth_are_the_unusable_ones() -> None:
    assert {c.id for c in CASES if c.expected is None} == {"low_quality", "not_an_invoice"}


@pytest.mark.parametrize("case", [c for c in CASES if c.expected is not None], ids=lambda c: c.id)
def test_ground_truth_satisfies_the_invoice_contract(case) -> None:
    invoice = InvoiceExtraction.model_validate(case.expected.as_contract_payload())
    # A representative invoice knows all of its critical fields.
    for name in CRITICAL_FIELDS:
        assert getattr(invoice, name).value is not None, (case.id, name)
    # Confidence is never invented by ground truth.
    assert all(getattr(invoice, n).confidence is None for n in CRITICAL_FIELDS)


def test_incomplete_case_actually_omits_optional_fields() -> None:
    expected = CASES_BY_ID["digital_incomplete"].expected
    assert expected.due_date is None
    assert expected.vendor_tax_id is None
    assert expected.customer_name is None
    assert expected.subtotal is None and expected.tax_amount is None
    assert expected.line_items[0].quantity is None


def test_scanned_case_is_a_non_blank_image_only_invoice() -> None:
    case = CASES_BY_ID["scanned_no_text"]
    reader = PdfReader(case.path)
    page = reader.pages[0]

    assert (page.extract_text() or "").strip() == ""
    xobjects = page["/Resources"]["/XObject"]
    images = [
        obj.get_object()
        for obj in xobjects.values()
        if obj.get_object()["/Subtype"] == "/Image"
    ]
    assert len(images) == 1
    # A blank white placeholder compresses to almost nothing and would make the
    # recorded ground truth impossible to recover.  Require visible dark ink.
    pixels = images[0].get_data()
    assert min(pixels) == 0 and max(pixels) == 255
    assert pixels.count(0) > 1_000


def test_multi_line_item_case_has_several_items_that_sum_to_subtotal() -> None:
    from decimal import Decimal

    expected = CASES_BY_ID["digital_multi_line_items"].expected
    assert len(expected.line_items) >= 3
    assert sum(Decimal(i.line_total) for i in expected.line_items) == Decimal(expected.subtotal)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_pdf_behaviour_matches_the_declared_text_layer(case) -> None:
    data = case.path.read_bytes()

    if case.text_layer == "UNREADABLE":
        with pytest.raises(Exception):
            PdfReader(BytesIO(data), strict=False).pages[0].extract_text()
        return

    reader = PdfReader(BytesIO(data), strict=False)
    text = "\n".join((page.extract_text() or "") for page in reader.pages)

    if case.text_layer == "DIGITAL":
        assert len(text.strip()) > 40
        if case.expected is not None:
            assert case.expected.invoice_number in text
            assert case.expected.total_amount in text
    else:  # ABSENT
        assert text.strip() == ""

    if case.id == "digital_multi_page":
        assert len(reader.pages) == 2


def test_committed_pdfs_match_a_fresh_generation() -> None:
    fresh = build_all()
    for case in CASES:
        assert case.path.read_bytes() == fresh[case.id], (
            f"{case.id}: committed PDF is stale - run "
            f"`python -m evaluation.generate_invoices`"
        )
