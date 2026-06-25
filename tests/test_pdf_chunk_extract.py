"""Hermetic tests for large-PDF page-chunked extraction (issue #16)."""

from __future__ import annotations

import pytest

from invoice_processing.extract.ledger_extract import (
    ExtractedDocument,
    ExtractedDocumentBundle,
    ExtractedDocumentLine,
)
from invoice_processing.extract.pdf_chunks import (
    CHUNK_PAGE_SIZE,
    LARGE_PDF_BYTE_THRESHOLD,
    LARGE_PDF_PAGE_THRESHOLD,
    iter_pdf_page_chunks,
    merge_chunk_bundles,
    should_chunk_pdf,
)

pytestmark = pytest.mark.unit


def _minimal_pdf_two_pages() -> bytes:
    """Tiny valid PDF with two pages (built via pypdfium2)."""
    import pypdfium2 as pdfium

    src = pdfium.PdfDocument.new()
    for _ in range(2):
        src.new_page(200, 200)
    import tempfile
    import os
    from pathlib import Path

    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    src.save(path)
    data = Path(path).read_bytes()
    os.unlink(path)
    src.close()
    return data


def _doc(reference: str, page_range: list[int]) -> ExtractedDocument:
    return ExtractedDocument(
        doc_type="receipt",
        page_range=page_range,
        vendor="Shop",
        reference=reference,
        date="2025-01-01",
        currency="SGD",
        presentation="summary",
        lines=[ExtractedDocumentLine(description="Item", net_amount=10.0, gst_amount=0.0)],
        subtotal=10.0,
        tax_total=0.0,
        grand_total=10.0,
        tax_lines=[],
        direction_for_client="purchase",
        tax_visible_on_document=False,
    )


def test_should_chunk_pdf_when_page_count_exceeds_threshold() -> None:
    assert should_chunk_pdf(b"x", "application/pdf", page_count=LARGE_PDF_PAGE_THRESHOLD + 1)
    assert not should_chunk_pdf(b"x", "application/pdf", page_count=LARGE_PDF_PAGE_THRESHOLD)


def test_should_chunk_pdf_when_byte_size_exceeds_threshold() -> None:
    big = b"x" * (LARGE_PDF_BYTE_THRESHOLD + 1)
    assert should_chunk_pdf(big, "application/pdf", page_count=1)
    assert not should_chunk_pdf(b"small", "image/png", page_count=1)


def test_iter_pdf_page_chunks_splits_pages() -> None:
    data = _minimal_pdf_two_pages()
    chunks = iter_pdf_page_chunks(data, chunk_size=1)
    assert len(chunks) == 2
    assert chunks[0][1] == 1 and chunks[0][2] == 1
    assert chunks[1][1] == 2 and chunks[1][2] == 2
    assert all(len(chunk_bytes) > 100 for chunk_bytes, _, _ in chunks)


def test_merge_chunk_bundles_offsets_page_ranges() -> None:
    bundle_a = ExtractedDocumentBundle(
        documents=[_doc("R-1", [1, 1])],
        skipped_pages=[2],
    )
    bundle_b = ExtractedDocumentBundle(
        documents=[_doc("R-2", [1, 2])],
        skipped_pages=None,
    )
    merged = merge_chunk_bundles([(bundle_a, 1), (bundle_b, 3)])
    assert len(merged.documents) == 2
    assert merged.documents[0].page_range == [1, 1]
    assert merged.documents[1].page_range == [3, 4]
    assert merged.skipped_pages == [2]


def test_chunk_page_size_is_reasonable_for_multi_receipt() -> None:
    assert CHUNK_PAGE_SIZE >= 3
    assert CHUNK_PAGE_SIZE <= 10
