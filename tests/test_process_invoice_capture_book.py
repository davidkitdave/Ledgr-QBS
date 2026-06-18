"""Hermetic tests for capture→book→verify orchestration."""

from __future__ import annotations


import pytest

from invoice_processing.extract.book import BookingLedgerLine, BookingProposal
from invoice_processing.extract.document_record import DocumentRecord, DocumentRecordBundle, LabeledField, LineCapture
from invoice_processing.extract.process_invoice_document import process_invoice_document

pytestmark = pytest.mark.unit


def test_process_invoice_document_capture_book_path(monkeypatch):
    capture = DocumentRecord(
        labeled_fields=[LabeledField(label="Invoice Number", value="EXP25-D03")],
        line_items=[
            LineCapture(description="Transport", net_amount=100.0),
        ],
        totals=[LabeledField(label="Total", value="100.00")],
    )
    bundle = DocumentRecordBundle(documents=[capture])

    def fake_extract(data, mime_type, **kwargs):
        return bundle

    def fake_book(record, **kwargs):
        return BookingProposal(
            doc_kind="expense_claim",
            direction_for_client="purchase",
            direction_reason="Employee reimbursement payable",
            ledger_lines=[BookingLedgerLine(description="Transport", net_amount=100.0)],
            invoice_number="EXP25-D03",
            document_date="2025-03-16",
            document_total=100.0,
            tax_visible_on_document=False,
        )

    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.EXTRACT_DOCUMENT_FN",
        fake_extract,
    )
    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.BOOK_FROM_CAPTURE_FN",
        fake_book,
    )
    monkeypatch.setenv("LEDGR_CAPTURE_BOOK", "1")

    result = process_invoice_document(
        b"%PDF",
        "application/pdf",
        doc_type="invoice",
        direction="auto",
        our_gst_registered=True,
    )
    assert result.extraction_path == "capture_book"
    assert len(result.normalized) == 1
    inv = result.normalized[0]
    assert inv.doc_type == "purchase"
    assert inv.tax_visible_on_document is False
    assert inv.direction_reason == "Employee reimbursement payable"
    assert inv.reconciled is True
