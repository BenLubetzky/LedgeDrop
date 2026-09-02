"""Tests for PDF preprocessing (Stage 3, step 8).

Preprocessing only reads the stored original and never calls an AI provider, so
these tests need storage but no database and no network.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.document import Document
from app.services.processing.extraction.preprocessing import (
    MIN_USEFUL_CHARS_PER_PAGE,
    PreprocessingError,
    PreprocessingErrorCode,
    TextLayer,
    prepare_document,
)

from tests._helpers import make_pdf, make_text_pdf

_LONG = "This invoice line carries well over sixteen characters of real text."


def _document(location: str) -> Document:
    return Document(
        document_id=uuid.uuid4(),
        original_filename="invoice.pdf",
        file_location=location,
        file_hash="a" * 64,
        file_size_bytes=1234,
        page_count=1,
    )


async def _store(storage, data: bytes) -> Document:
    doc = _document("pending")
    stored = await storage.save_bytes(doc.document_id, data)
    doc.file_location = stored.location
    return doc


# --- digital text -------------------------------------------------------


async def test_digital_pdf_text_is_extracted_per_page(storage) -> None:
    pdf = make_text_pdf([f"Page one. {_LONG}", f"Page two. {_LONG}"])
    doc = await _store(storage, pdf)

    prepared = await prepare_document(doc, storage)

    assert prepared.page_count == 2
    assert prepared.text_layer is TextLayer.DIGITAL
    assert prepared.needs_ocr is False
    assert prepared.ocr_page_numbers == ()
    assert [p.number for p in prepared.pages] == [1, 2]
    assert all(p.has_text_layer for p in prepared.pages)
    assert "Page one." in prepared.pages[0].text
    assert "Page two." in prepared.pages[1].text
    assert "\f" in prepared.text  # pages joined by form feed
    assert prepared.char_count == sum(p.char_count for p in prepared.pages)


async def test_prepared_document_keeps_the_original_bytes_for_later_ocr(storage) -> None:
    pdf = make_text_pdf([f"Only page. {_LONG}"])
    doc = await _store(storage, pdf)

    prepared = await prepare_document(doc, storage)

    assert prepared.pdf_bytes == pdf
    # ...and the stored file itself is untouched.
    assert (await storage.path_for(doc.file_location)).read_bytes() == pdf


# --- scanned / no useful text ----------------------------------------


async def test_blank_scan_has_no_text_layer_and_needs_ocr_on_every_page(storage) -> None:
    doc = await _store(storage, make_pdf(3))

    prepared = await prepare_document(doc, storage)

    assert prepared.text_layer is TextLayer.ABSENT
    assert prepared.needs_ocr is True
    assert prepared.ocr_page_numbers == (1, 2, 3)
    assert prepared.text.replace("\f", "") == ""
    assert all(p.needs_ocr for p in prepared.pages)


async def test_page_with_only_a_few_characters_is_treated_as_needing_ocr(storage) -> None:
    assert len("Inv 1") < MIN_USEFUL_CHARS_PER_PAGE
    doc = await _store(storage, make_text_pdf(["Inv 1"]))

    prepared = await prepare_document(doc, storage)

    assert prepared.text_layer is TextLayer.ABSENT
    assert prepared.pages[0].needs_ocr is True


async def test_mixed_document_is_partial_and_lists_only_the_scanned_pages(storage) -> None:
    pdf = make_text_pdf([f"Real text page. {_LONG}", "", f"Another real page. {_LONG}"])
    doc = await _store(storage, pdf)

    prepared = await prepare_document(doc, storage)

    assert prepared.text_layer is TextLayer.PARTIAL
    assert prepared.needs_ocr is True
    assert prepared.ocr_page_numbers == (2,)
    assert prepared.pages[0].has_text_layer and prepared.pages[2].has_text_layer


# --- error paths -------------------------------------------------------


async def test_missing_stored_file_raises_pdf_unavailable(storage) -> None:
    doc = _document(storage.location_for(uuid.uuid4()))  # nothing was ever written

    with pytest.raises(PreprocessingError) as excinfo:
        await prepare_document(doc, storage)
    assert excinfo.value.code is PreprocessingErrorCode.PDF_UNAVAILABLE


async def test_non_pdf_bytes_raise_pdf_unreadable(storage) -> None:
    doc = await _store(storage, b"this is definitely not a pdf")

    with pytest.raises(PreprocessingError) as excinfo:
        await prepare_document(doc, storage)
    assert excinfo.value.code is PreprocessingErrorCode.PDF_UNREADABLE


async def test_corrupt_pdf_raises_pdf_unreadable(storage) -> None:
    doc = await _store(storage, b"%PDF-1.4\n%%EOF only, no objects at all")

    with pytest.raises(PreprocessingError) as excinfo:
        await prepare_document(doc, storage)
    assert excinfo.value.code is PreprocessingErrorCode.PDF_UNREADABLE


async def test_page_count_over_the_limit_is_rejected(storage) -> None:
    doc = await _store(storage, make_pdf(4))

    with pytest.raises(PreprocessingError) as excinfo:
        await prepare_document(doc, storage, max_pages=3)
    assert excinfo.value.code is PreprocessingErrorCode.TOO_MANY_PAGES


async def test_path_traversal_location_is_refused(storage) -> None:
    doc = _document("../../etc/passwd")

    with pytest.raises(PreprocessingError) as excinfo:
        await prepare_document(doc, storage)
    assert excinfo.value.code is PreprocessingErrorCode.PDF_UNAVAILABLE
