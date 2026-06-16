"""Tests for Drive-parity ledger extract mapper (no LLM)."""

from __future__ import annotations

from invoice_processing.extract.ledger_extract import (
    DocumentLedgerExtract,
    LedgerLine,
    SummaryField,
    ledger_extract_to_extracted_invoice,
    ledger_extract_to_normalized,
    should_use_legacy_extract,
    use_understand_extract,
    validate_ledger_extract,
)


def test_should_use_legacy_for_soa():
    assert should_use_legacy_extract("statement_of_account") is True
    assert should_use_legacy_extract("invoice") is False


def test_use_understand_extract_defaults_on(monkeypatch):
    monkeypatch.delenv("LEDGR_UNDERSTAND_EXTRACT", raising=False)
    assert use_understand_extract() is True
    monkeypatch.setenv("LEDGR_UNDERSTAND_EXTRACT", "0")
    assert use_understand_extract() is False


def test_mapper_sample_test_group_shape():
    extract = DocumentLedgerExtract(
        vendor_name="Sample Vendor Pte Ltd",
        customer_name="Acme Client Pte Ltd",
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
