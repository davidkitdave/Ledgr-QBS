"""Hermetic tests for understand-path multi-document fan-out (WS-2.1)."""

from __future__ import annotations

import pytest

from invoice_processing.extract.ledger_extract import (
    ExtractedDocument,
    ExtractedDocumentBundle,
    ExtractedDocumentLine,
)
from invoice_processing.extract.process_invoice_document import process_invoice_document

pytestmark = pytest.mark.unit


def _doc(
    reference: str,
    *,
    grand_total: float,
    page_range: list[int] | None = None,
) -> ExtractedDocument:
    return ExtractedDocument(
        doc_type="invoice",
        page_range=page_range or [1, 1],
        vendor="Vendor-A",
        buyer="Buyer-B",
        reference=reference,
        date="2025-12-01",
        currency="MYR",
        presentation="summary",
        lines=[
            ExtractedDocumentLine(
                description="Parts",
                net_amount=grand_total,
                gst_amount=0.0,
            ),
        ],
        subtotal=grand_total,
        tax_total=0.0,
        grand_total=grand_total,
        tax_lines=[],
        direction_for_client="purchase",
        tax_visible_on_document=False,
    )


def test_process_invoice_document_understand_fans_out_two_docs(monkeypatch):
    bundle = ExtractedDocumentBundle(
        documents=[
            _doc("INV-200", grand_total=200.0, page_range=[1, 1]),
            _doc("INV-060", grand_total=60.0, page_range=[2, 2]),
        ],
        skipped_pages=None,
    )

    def fake_extract(data, mime_type, **kwargs):
        return bundle

    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.EXTRACT_LEDGER_FN",
        fake_extract,
    )
    monkeypatch.delenv("LEDGR_CAPTURE_BOOK", raising=False)
    monkeypatch.setenv("LEDGR_UNDERSTAND_EXTRACT", "1")

    result = process_invoice_document(
        b"%PDF",
        "application/pdf",
        doc_type="invoice",
        direction="purchase",
        our_gst_registered=True,
        base_currency="MYR",
    )

    assert result.extraction_path == "understand"
    assert len(result.normalized) == 2
    totals = sorted(inv.doc_total for inv in result.normalized if inv.doc_total is not None)
    assert totals == [60.0, 200.0]
    refs = {inv.invoice_number for inv in result.normalized}
    assert refs == {"INV-200", "INV-060"}


def test_process_invoice_document_understand_reconcile_per_doc(monkeypatch):
    """WS-2.2 — each bundle doc runs reconcile(); mismatches flag reconciled=False."""
    good = _doc("INV-OK", grand_total=200.0, page_range=[1, 1])
    bad = ExtractedDocument(
        doc_type="invoice",
        page_range=[2, 2],
        vendor="Vendor-B",
        buyer="Buyer-B",
        reference="INV-BAD",
        date="2025-12-01",
        currency="MYR",
        presentation="summary",
        lines=[
            ExtractedDocumentLine(
                description="Parts",
                net_amount=100.0,
                gst_amount=0.0,
            ),
        ],
        subtotal=100.0,
        tax_total=0.0,
        grand_total=99.0,
        tax_lines=[],
        direction_for_client="purchase",
        tax_visible_on_document=False,
    )
    bundle = ExtractedDocumentBundle(documents=[good, bad], skipped_pages=None)

    def fake_extract(data, mime_type, **kwargs):
        return bundle

    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.EXTRACT_LEDGER_FN",
        fake_extract,
    )
    monkeypatch.delenv("LEDGR_CAPTURE_BOOK", raising=False)
    monkeypatch.setenv("LEDGR_UNDERSTAND_EXTRACT", "1")

    result = process_invoice_document(
        b"%PDF",
        "application/pdf",
        doc_type="invoice",
        direction="purchase",
        our_gst_registered=True,
        base_currency="MYR",
    )

    by_ref = {inv.invoice_number: inv for inv in result.normalized}
    assert by_ref["INV-OK"].reconciled is True
    assert by_ref["INV-OK"].reconcile_note
    assert by_ref["INV-BAD"].reconciled is False
    assert "total" in (by_ref["INV-BAD"].reconcile_note or "").lower()
