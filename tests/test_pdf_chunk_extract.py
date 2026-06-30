"""Hermetic tests for large-PDF page-chunked extraction (issue #16).

The pre-chunk gate at ``should_chunk_pdf`` is intentionally narrow after the
Phase-0 control experiment on ``feat/minimal-extract-control-experiment``:
it returns True only when the PDF would exceed Google's documented 50 MB
inline limit. Page count alone does NOT trigger chunking — see
``docs/agents/issue-tracker.md`` / issue #28 for the rationale. The chunked
extraction helpers (``iter_pdf_page_chunks``, ``merge_chunk_bundles``) remain
in use by the ``ValidationError`` fallback in ``ledger_extract.py``.
"""

from __future__ import annotations

import pytest

from invoice_processing.extract.ledger_extract import (
    ExtractedDocument,
    ExtractedDocumentBundle,
    ExtractedDocumentLine,
)
from invoice_processing.extract.pdf_chunks import (
    CHUNK_PAGE_SIZE,
    INLINE_PDF_BYTE_LIMIT,
    LARGE_PDF_BYTE_THRESHOLD,
    iter_pdf_page_chunks,
    merge_chunk_bundles,
    should_chunk_pdf,
)
from invoice_processing.shared_libraries.gemini_call_config import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    default_llm_config,
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


def test_should_chunk_pdf_byte_size_exceeds_limit() -> None:
    """Hard limit: chunk only when bytes exceed Google's 50 MB inline cap."""
    big = b"x" * (LARGE_PDF_BYTE_THRESHOLD + 1)
    assert should_chunk_pdf(big, "application/pdf", page_count=1)
    # Just under the threshold stays inline.
    small = b"x" * (LARGE_PDF_BYTE_THRESHOLD - 1)
    assert not should_chunk_pdf(small, "application/pdf", page_count=1)
    # Non-PDF MIME never chunks.
    assert not should_chunk_pdf(b"small", "image/png", page_count=1)


def test_should_chunk_pdf_page_count_alone_never_triggers_chunk() -> None:
    """Regression: page count must NOT pre-chunk after the Phase-0 fix.

    The Starhub bill (18 pages, 4.4 MB) and multi-receipt PDF (35 pages,
    19.4 MB) both go through one Gemini call now — see
    ``scripts/spike_minimal_extract_vs_pipeline.py`` for the A/B proof.
    """
    # Starhub-like: 18 pages, 4.4 MB.
    assert not should_chunk_pdf(b"x" * (4 * 1024 * 1024), "application/pdf", page_count=18)
    # Multi-receipt-like: 35 pages, 19.4 MB.
    assert not should_chunk_pdf(b"x" * (19 * 1024 * 1024), "application/pdf", page_count=35)
    # Even an absurd 999-page skinny PDF stays inline (well under 50 MB).
    assert not should_chunk_pdf(b"x" * (10 * 1024 * 1024), "application/pdf", page_count=999)


def test_inline_pdf_byte_limit_matches_google_documented_ceiling() -> None:
    """Google's documented inline PDF cap is 50 MB; the default must match."""
    assert INLINE_PDF_BYTE_LIMIT == 50 * 1024 * 1024
    assert LARGE_PDF_BYTE_THRESHOLD == INLINE_PDF_BYTE_LIMIT


def test_default_llm_config_pins_max_output_tokens() -> None:
    """Output budget must be set so a single call can hold a full 35-page JSON.

    Without ``max_output_tokens`` the SDK default was too low and silently
    truncated multi-receipt output (issue #16 / #28). The Phase-0 spike
    proved that with ``DEFAULT_MAX_OUTPUT_TOKENS=65536`` all 852 lines for a
    35-page / 19.4 MB PDF fit in one call.
    """
    assert DEFAULT_MAX_OUTPUT_TOKENS == 65536
    cfg = default_llm_config()
    assert cfg.max_output_tokens == 65536
    # Caller overrides win.
    cfg = default_llm_config(max_output_tokens=8192)
    assert cfg.max_output_tokens == 8192


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


def test_chunked_extract_drops_soa_cover_after_merge(monkeypatch) -> None:
    """SOA cover in chunk 1 alone is not dropped per-chunk; merge must drop it."""
    from invoice_processing.extract.ledger_extract import (
        _drop_soa_cover_documents,
        extract_document_ledger,
    )

    cover = ExtractedDocument(
        doc_type="statement",
        page_range=[1, 1],
        vendor="Vendor",
        reference="SOA-1",
        date="2026-01-01",
        currency="SGD",
        grand_total=100.0,
    )
    invoice = ExtractedDocument(
        doc_type="invoice",
        page_range=[1, 1],
        vendor="Vendor",
        reference="INV-1",
        date="2026-01-01",
        currency="SGD",
        presentation="summary",
        lines=[ExtractedDocumentLine(description="Item", net_amount=100.0, gst_amount=0.0)],
        subtotal=100.0,
        tax_total=0.0,
        grand_total=100.0,
        tax_lines=[],
        direction_for_client="purchase",
        tax_visible_on_document=False,
    )

    def _fake_chunked(_data, _mime, **kwargs):
        from invoice_processing.extract.ledger_extract import ExtractedDocumentBundle

        return ExtractedDocumentBundle(
            documents=[cover, invoice],
            skipped_pages=None,
        )

    monkeypatch.setattr(
        "invoice_processing.extract.pdf_chunks.extract_document_ledger_chunked",
        _fake_chunked,
    )
    # Drive the chunked path via the byte-guard rather than the removed page guard.
    monkeypatch.setattr(
        "invoice_processing.extract.pdf_chunks.should_chunk_pdf",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        "invoice_processing.extract.segmentation_gates.count_input_pages",
        lambda *_a, **_k: 20,
    )

    bundle = extract_document_ledger(b"x", "application/pdf")
    assert len(bundle.documents) == 1
    assert bundle.documents[0].doc_type == "invoice"
    assert bundle.skipped_pages == [1]

    # Sanity: per-chunk-only drop would leave the cover when chunk has one doc.
    lone_cover = ExtractedDocumentBundle(documents=[cover])
    assert len(_drop_soa_cover_documents(lone_cover).documents) == 1
