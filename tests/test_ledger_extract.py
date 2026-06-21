"""Tests for Drive-parity ledger extract mapper (no LLM)."""

from __future__ import annotations

from invoice_processing.extract.ledger_extract import (
    DocumentLedgerExtract,
    ExtractedDocument,
    ExtractedDocumentLine,
    LedgerLine,
    SummaryField,
    extracted_document_to_normalized,
    ledger_extract_to_extracted_invoice,
    ledger_extract_to_normalized,
    should_use_legacy_extract,
    use_capture_book_pipeline,
    use_legacy_soa,
    use_understand_extract,
    validate_extracted_document,
    validate_ledger_extract,
)


def test_should_use_legacy_soa_quarantined(monkeypatch):
    monkeypatch.delenv("LEDGR_LEGACY_SOA", raising=False)
    assert use_legacy_soa() is False
    assert should_use_legacy_extract("statement_of_account") is False
    assert should_use_legacy_extract("invoice") is False

    monkeypatch.setenv("LEDGR_LEGACY_SOA", "1")
    assert use_legacy_soa() is True
    assert should_use_legacy_extract("statement_of_account") is True
    assert should_use_legacy_extract("invoice") is False


def test_understand_disabled_does_not_route_invoice_to_legacy(monkeypatch):
    """WS-5.1: LEDGR_UNDERSTAND_EXTRACT=0 must not accidentally fall back to legacy."""
    monkeypatch.setenv("LEDGR_UNDERSTAND_EXTRACT", "0")
    monkeypatch.delenv("LEDGR_LEGACY_SOA", raising=False)
    monkeypatch.delenv("LEDGR_CAPTURE_BOOK", raising=False)
    assert should_use_legacy_extract("invoice") is False
    assert should_use_legacy_extract("receipt") is False
    assert should_use_legacy_extract("statement_of_account") is False


def test_use_understand_extract_defaults_on(monkeypatch):
    monkeypatch.delenv("LEDGR_UNDERSTAND_EXTRACT", raising=False)
    assert use_understand_extract() is True
    monkeypatch.setenv("LEDGR_UNDERSTAND_EXTRACT", "0")
    assert use_understand_extract() is False


def test_use_capture_book_defaults_off(monkeypatch):
    monkeypatch.delenv("LEDGR_CAPTURE_BOOK", raising=False)
    assert use_capture_book_pipeline() is False
    monkeypatch.setenv("LEDGR_CAPTURE_BOOK", "1")
    assert use_capture_book_pipeline() is True


def test_tax_visible_propagates_to_normalized():
    extract = DocumentLedgerExtract(
        vendor_name="Acme Vendor",
        document_reference="INV-1",
        document_date="2026-06-17",
        currency="SGD",
        document_total=100.0,
        subtotal=100.0,
        gst_total=0.0,
        tax_visible_on_document=False,
        ledger_lines=[
            LedgerLine(description="Service", net_amount=100.0, gst_amount=0.0),
        ],
        summary_table=[],
    )
    inv = ledger_extract_to_normalized(extract, direction="purchase")
    assert inv.tax_visible_on_document is False


def test_mapper_sample_test_group_shape():
    extract = DocumentLedgerExtract(
        vendor_name="Sample Vendor Pte Ltd",
        customer_name="Company-A",
        document_reference="2026/0210",
        document_date="2026-05-01",
        currency="SGD",
        document_total=1500.0,
        subtotal=1500.0,
        gst_total=0.0,
        summary_table=[
            SummaryField(category="Vendor Name", details="Sample Vendor Pte Ltd"),
            SummaryField(category="Invoice Number", details="2026/0210"),
        ],
        ledger_lines=[
            LedgerLine(
                description="Management fees",
                net_amount=1500.0,
                gst_amount=0.0,
                tax_hint="NT",
            ),
        ],
    )
    ex = ledger_extract_to_extracted_invoice(extract)
    assert ex.invoice_number == "2026/0210"
    assert len(ex.lines) == 1
    ok, _ = validate_ledger_extract(extract)
    assert ok

    inv = ledger_extract_to_normalized(
        extract, direction="purchase", our_gst_registered=False
    )
    assert inv.invoice_number == "2026/0210"
    assert len(inv.lines) == 1
    assert inv.reconciled


def test_mapper_telco_two_lines():
    extract = DocumentLedgerExtract(
        vendor_name="Telco Provider A Ltd",
        document_reference="8004483920122025",
        document_date="2025-12-04",
        currency="SGD",
        document_total=1328.15,
        subtotal=1223.35,
        gst_total=104.80,
        ledger_lines=[
            LedgerLine(
                description="Telecommunication services - standard rated (9%)",
                net_amount=1164.42,
                gst_amount=104.80,
                tax_hint="SR",
            ),
            LedgerLine(
                description="Telecommunication services - zero rated",
                net_amount=58.93,
                gst_amount=0.0,
                tax_hint="ZR",
            ),
        ],
        summary_table=[
            SummaryField(category="Current Charges", details="$1,328.15"),
        ],
    )
    ok, detail = validate_ledger_extract(extract)
    assert ok, detail
    inv = ledger_extract_to_normalized(
        extract, direction="purchase", our_gst_registered=False
    )
    assert len(inv.lines) == 2
    assert inv.doc_total == 1328.15


def test_expense_claim_claimant_becomes_issuer():
    """For expense_claim, claimant_name wins over from_party.name as issuer.

    This is the data-shape plumbing the prompt teaches the model — no Python
    rule switch downstream (ADR-0015). Round-trips through the mapper without
    flipping direction.
    """
    extract = DocumentLedgerExtract(
        vendor_name="Company A Pte Ltd",
        customer_name="Company A Pte Ltd",
        document_reference="EXP-2026-001",
        document_date="2026-03-16",
        currency="SGD",
        document_total=1195.11,
        subtotal=1195.11,
        gst_total=0.0,
        doc_kind="expense_claim",
        claimant_name="Person 1",
        tax_visible_on_document=False,
        direction_for_client="purchase",
        direction_reason="claimant signed the form; client is the approver",
        ledger_lines=[
            LedgerLine(description="Transportation", net_amount=263.85, gst_amount=0.0),
            LedgerLine(description="Accommodations", net_amount=274.14, gst_amount=0.0),
            LedgerLine(description="Travel per diem", net_amount=600.00, gst_amount=0.0),
            LedgerLine(description="Taxi Fee Yangon", net_amount=57.12, gst_amount=0.0),
        ],
        summary_table=[],
    )
    ex = ledger_extract_to_extracted_invoice(extract)
    # Claimant wins over letterhead for the supplier/issuer slot.
    assert ex.issuer_name == "Person 1"
    assert ex.doc_type == "invoice"
    assert ex.total == 1195.11
    inv = ledger_extract_to_normalized(
        extract, direction="purchase", our_gst_registered=False
    )
    assert inv.tax_visible_on_document is False
    # Non-GST-registered client + tax_visible=False -> every line NT.
    from invoice_processing.export.tax_classifier import classify_invoice
    classify_invoice(inv)
    for line in inv.lines:
        assert line.tax_treatment == "NT"


def test_doc_kind_default_is_invoice_and_claimant_none():
    """Backward compatibility: omitting the new fields works as before."""
    extract = DocumentLedgerExtract(
        vendor_name="Acme Vendor",
        document_reference="INV-1",
        document_date="2026-06-17",
        currency="SGD",
        document_total=100.0,
        subtotal=100.0,
        gst_total=0.0,
        ledger_lines=[LedgerLine(description="Service", net_amount=100.0, gst_amount=0.0)],
    )
    assert extract.doc_kind == "invoice"
    assert extract.claimant_name is None
    assert extract.direction_reason is None
    assert extract.tax_visible_on_document is False


def test_currency_propagates_to_normalized_and_exporter():
    """A USD expense claim must stay USD through the pipeline (not silently SGD).

    Reproduces the screenshot bug: extraction's ``currency`` field must
    reach the Xero exporter's ``Currency`` column unchanged, even when
    the client's base_currency is SGD. This is the plumbing test for
    the F-cluster ``currency_routing_score`` gate.
    """
    extract = DocumentLedgerExtract(
        vendor_name="Person-1",
        document_reference="EXP-1",
        document_date="2026-06-17",
        currency="USD",
        document_total=1195.11,
        subtotal=1195.11,
        gst_total=0.0,
        doc_kind="expense_claim",
        claimant_name="Person-1",
        tax_visible_on_document=False,
        direction_for_client="purchase",
        direction_reason="claimant signed the form; client is the approver",
        ledger_lines=[
            LedgerLine(description="Transportation", net_amount=131.77, gst_amount=0.0),
            LedgerLine(description="Accommodations", net_amount=274.14, gst_amount=0.0),
        ],
    )
    inv = ledger_extract_to_normalized(
        extract,
        direction="purchase",
        our_gst_registered=False,
        base_currency="SGD",
    )
    # Currency survives the mapper, even though the client books in SGD.
    assert inv.currency == "USD"

    from invoice_processing.export.exporters import XeroLedgerExporter

    rows = XeroLedgerExporter().rows([inv], "purchase")
    assert rows, "expected at least one Xero row for the expense claim"
    assert rows[0]["Currency"] == "USD"


def test_extracted_document_mapper_single_doc():
    doc = ExtractedDocument(
        doc_type="invoice",
        page_range=[1, 1],
        vendor="Vendor-A",
        buyer="Buyer-B",
        reference="INV-42",
        date="2026-06-01",
        currency="MYR",
        presentation="summary",
        lines=[ExtractedDocumentLine(description="Parts", net_amount=60.0, gst_amount=0.0)],
        subtotal=60.0,
        tax_total=0.0,
        grand_total=60.0,
        direction_for_client="purchase",
        tax_visible_on_document=False,
    )
    ok, detail = validate_extracted_document(doc)
    assert ok, detail
    inv = extracted_document_to_normalized(doc, direction="purchase", base_currency="MYR")
    assert inv.invoice_number == "INV-42"
    assert inv.doc_total == 60.0


def test_validate_extracted_document_g4_tolerance_within_two_cents():
    """G4: MYR docs within 2-cent integer tolerance reconcile."""
    doc = ExtractedDocument(
        doc_type="invoice",
        page_range=[1, 1],
        vendor="Vendor-A",
        reference="INV-1",
        date="2026-06-01",
        currency="MYR",
        lines=[ExtractedDocumentLine(description="Parts", net_amount=100.0, gst_amount=0.0)],
        subtotal=100.0,
        tax_total=0.0,
        grand_total=100.02,
        direction_for_client="purchase",
        tax_visible_on_document=False,
    )
    ok, detail = validate_extracted_document(doc)
    assert ok, detail


def test_validate_extracted_document_g4_tolerance_flags_three_cent_gap():
    """G4: gaps beyond 2 cents fail reconcile."""
    doc = ExtractedDocument(
        doc_type="invoice",
        page_range=[1, 1],
        vendor="Vendor-A",
        reference="INV-1",
        date="2026-06-01",
        currency="MYR",
        lines=[ExtractedDocumentLine(description="Parts", net_amount=100.0, gst_amount=0.0)],
        subtotal=100.0,
        tax_total=0.0,
        grand_total=100.03,
        direction_for_client="purchase",
        tax_visible_on_document=False,
    )
    ok, detail = validate_extracted_document(doc)
    assert not ok
    assert "total" in detail.lower()
