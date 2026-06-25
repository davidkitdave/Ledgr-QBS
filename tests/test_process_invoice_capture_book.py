"""Hermetic tests — Capture→Book→Verify retired from live routing."""

from __future__ import annotations

import pytest

from invoice_processing.extract.ledger_extract import ExtractedDocument, ExtractedDocumentBundle
from invoice_processing.extract.process_invoice_document import process_invoice_document

pytestmark = pytest.mark.unit


def _single_doc_bundle() -> ExtractedDocumentBundle:
    return ExtractedDocumentBundle(
        documents=[
            ExtractedDocument(
                doc_type="invoice",
                page_range=[1, 1],
                vendor="Vendor-A",
                reference="INV-1",
                date="2026-06-01",
                currency="SGD",
                lines=[],
                subtotal=100.0,
                tax_total=0.0,
                grand_total=100.0,
                direction_for_client="purchase",
                tax_visible_on_document=False,
            )
        ],
    )


def test_capture_book_env_ignored_uses_understand(monkeypatch):
    """Retired: LEDGR_CAPTURE_BOOK no longer switches routing."""
    ledger_called = {"count": 0}

    def fake_extract(*args, **kwargs):
        ledger_called["count"] += 1
        return _single_doc_bundle()

    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.EXTRACT_LEDGER_FN",
        fake_extract,
    )
    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.count_input_pages",
        lambda data, mime: 1,
    )
    monkeypatch.setenv("LEDGR_CAPTURE_BOOK", "1")

    result = process_invoice_document(
        b"%PDF",
        "application/pdf",
        doc_type="invoice",
        direction="auto",
        our_gst_registered=True,
    )
    assert result.extraction_path == "understand"
    assert ledger_called["count"] == 1
    assert len(result.normalized) == 1
