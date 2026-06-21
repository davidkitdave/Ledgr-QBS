"""Hermetic tests for WS-5.1 single default extraction path routing."""

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


def _install_understand_stub(monkeypatch, bundle: ExtractedDocumentBundle | None = None):
    captured = bundle or _single_doc_bundle()

    def fake_extract(*args, **kwargs):
        return captured

    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.EXTRACT_LEDGER_FN",
        fake_extract,
    )
    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.count_input_pages",
        lambda data, mime: 1,
    )


def test_default_invoice_uses_understand_path(monkeypatch):
    _install_understand_stub(monkeypatch)
    monkeypatch.delenv("LEDGR_CAPTURE_BOOK", raising=False)
    monkeypatch.delenv("LEDGR_LEGACY_SOA", raising=False)
    monkeypatch.delenv("LEDGR_UNDERSTAND_EXTRACT", raising=False)

    result = process_invoice_document(
        b"%PDF",
        "application/pdf",
        doc_type="invoice",
        direction="purchase",
    )
    assert result.extraction_path == "understand"


def test_understand_disabled_still_uses_understand_for_invoice(monkeypatch):
    """Disabling the env flag must not accidentally route invoices to legacy."""
    _install_understand_stub(monkeypatch)
    monkeypatch.setenv("LEDGR_UNDERSTAND_EXTRACT", "0")
    monkeypatch.delenv("LEDGR_CAPTURE_BOOK", raising=False)
    monkeypatch.delenv("LEDGR_LEGACY_SOA", raising=False)

    result = process_invoice_document(
        b"%PDF",
        "application/pdf",
        doc_type="invoice",
        direction="purchase",
    )
    assert result.extraction_path == "understand"


def test_soa_without_quarantine_switch_uses_understand(monkeypatch):
    _install_understand_stub(monkeypatch)
    monkeypatch.delenv("LEDGR_LEGACY_SOA", raising=False)
    monkeypatch.delenv("LEDGR_CAPTURE_BOOK", raising=False)

    result = process_invoice_document(
        b"%PDF",
        "application/pdf",
        doc_type="statement_of_account",
        direction="purchase",
    )
    assert result.extraction_path == "understand"


def test_soa_with_legacy_quarantine_uses_legacy(monkeypatch):
    from invoice_processing.extract.document_record import DocumentRecordBundle

    def fake_legacy_extract(data, mime_type, **kwargs):
        return DocumentRecordBundle(documents=[])

    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.EXTRACT_DOCUMENT_FN",
        fake_legacy_extract,
    )
    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.count_input_pages",
        lambda data, mime: 1,
    )
    monkeypatch.setenv("LEDGR_LEGACY_SOA", "1")
    monkeypatch.delenv("LEDGR_CAPTURE_BOOK", raising=False)

    result = process_invoice_document(
        b"%PDF",
        "application/pdf",
        doc_type="statement_of_account",
        direction="purchase",
    )
    assert result.extraction_path == "legacy"


def test_capture_book_not_default_for_invoice(monkeypatch):
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
    monkeypatch.delenv("LEDGR_CAPTURE_BOOK", raising=False)

    result = process_invoice_document(
        b"%PDF",
        "application/pdf",
        doc_type="invoice",
        direction="purchase",
    )
    assert result.extraction_path == "understand"
    assert ledger_called["count"] == 1
