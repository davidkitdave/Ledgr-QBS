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
    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.count_input_pages",
        lambda _data, _mime: 2,
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
    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.count_input_pages",
        lambda _data, _mime: 2,
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
    assert len(result.normalized) == 2
    assert any("partial extraction" in w for w in result.partial_failure_warnings)
    assert any("1 of 2" in w for w in result.partial_failure_warnings)


def test_process_invoice_document_understand_page_coverage_valid(monkeypatch):
    """WS-2.3 — full page coverage leaves docs reconciled when totals match."""
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
    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.count_input_pages",
        lambda _data, _mime: 2,
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

    assert all(inv.reconciled for inv in result.normalized)
    assert not any(
        "segmentation uncertain" in (inv.reconcile_note or "").lower()
        for inv in result.normalized
    )


def test_process_invoice_document_understand_page_coverage_gap_flags_all(monkeypatch):
    """WS-2.3 — gap in page_range flags segmentation uncertain on every doc."""
    bundle = ExtractedDocumentBundle(
        documents=[
            _doc("INV-A", grand_total=200.0, page_range=[1, 1]),
            _doc("INV-C", grand_total=60.0, page_range=[3, 3]),
        ],
        skipped_pages=None,
    )

    def fake_extract(data, mime_type, **kwargs):
        return bundle

    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.EXTRACT_LEDGER_FN",
        fake_extract,
    )
    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.count_input_pages",
        lambda _data, _mime: 3,
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

    assert len(result.normalized) == 2
    by_ref = {inv.invoice_number: inv for inv in result.normalized}
    for inv in result.normalized:
        note = (inv.reconcile_note or "").lower()
        assert "segmentation uncertain" in note
        assert "gaps" in note
    assert by_ref["INV-A"].reconciled is True
    assert by_ref["INV-C"].reconciled is True
    assert any("partial extraction" in w for w in result.partial_failure_warnings)
    assert any("gaps" in w for w in result.partial_failure_warnings)


def test_process_invoice_document_understand_page_coverage_overlap_flags_all(monkeypatch):
    """WS-2.3 — overlapping page_range flags segmentation uncertain."""
    bundle = ExtractedDocumentBundle(
        documents=[
            _doc("INV-A", grand_total=200.0, page_range=[1, 2]),
            _doc("INV-B", grand_total=60.0, page_range=[2, 3]),
        ],
        skipped_pages=None,
    )

    def fake_extract(data, mime_type, **kwargs):
        return bundle

    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.EXTRACT_LEDGER_FN",
        fake_extract,
    )
    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.count_input_pages",
        lambda _data, _mime: 3,
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

    for inv in result.normalized:
        assert inv.reconciled is True
        assert "segmentation uncertain" in (inv.reconcile_note or "").lower()
    assert any("partial extraction" in w for w in result.partial_failure_warnings)
