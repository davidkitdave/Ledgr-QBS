"""M1 verbatim-by-default line grouping (WS-2.6)."""

from __future__ import annotations

from invoice_processing.export.exporters import _load_erp_profile
from invoice_processing.export.line_grouping import telco_gst_bucket_lines
from invoice_processing.extract.document_normalizer import normalize_document_record
from invoice_processing.extract.ledger_extract import (
    ExtractedDocument,
    ExtractedDocumentLine,
    extracted_document_to_normalized,
    validate_extracted_document,
)
from tests.test_telco_summary import _telco_bill_a_capture


def test_m1_verbatim_default_telco_keeps_all_capture_lines():
    """Without an ERP profile, capture line_items pass through verbatim."""
    inv = normalize_document_record(
        _telco_bill_a_capture(),
        direction="purchase",
        our_gst_registered=True,
        mapper_version="enhanced",
    )
    assert len(inv.lines) == 300


def test_m1_erp_profile_declares_telco_grouping():
    """Telco SR/ZR collapse runs only when the ERP profile declares it."""
    profile = _load_erp_profile("autocount.yaml")
    inv = normalize_document_record(
        _telco_bill_a_capture(),
        direction="purchase",
        our_gst_registered=True,
        mapper_version="enhanced",
        erp_profile=profile,
    )
    assert len(inv.lines) == 2
    assert inv.lines[0].net_amount == 1164.42
    assert inv.lines[0].gst_amount == 104.80
    assert inv.lines[1].net_amount == 58.93
    assert inv.doc_total == 1328.15
    assert inv.reconciled is True


def test_m1_multi_line_parts_invoice_stays_itemized():
    """Multi-line trade invoice — each printed row survives the faithful path."""
    doc = ExtractedDocument(
        doc_type="invoice",
        page_range=[1, 1],
        vendor="Parts Vendor",
        buyer="Workshop-B",
        reference="INV-PARTS-99",
        date="2026-06-01",
        currency="MYR",
        presentation="itemized",
        lines=[
            ExtractedDocumentLine(
                description="Brake pad set",
                quantity=2.0,
                unit_amount=45.0,
                net_amount=90.0,
                gst_amount=0.0,
            ),
            ExtractedDocumentLine(
                description="Oil filter",
                quantity=1.0,
                unit_amount=22.5,
                net_amount=22.5,
                gst_amount=0.0,
            ),
            ExtractedDocumentLine(
                description="Labour — brake service",
                quantity=1.0,
                unit_amount=120.0,
                net_amount=120.0,
                gst_amount=0.0,
            ),
        ],
        subtotal=232.5,
        tax_total=0.0,
        grand_total=232.5,
        direction_for_client="purchase",
        tax_visible_on_document=False,
    )
    ok, detail = validate_extracted_document(doc)
    assert ok, detail
    inv = extracted_document_to_normalized(doc, direction="purchase", base_currency="MYR")
    assert doc.presentation == "itemized"
    assert len(inv.lines) == 3
    assert inv.lines[0].description == "Brake pad set"
    assert inv.lines[0].quantity == 2.0


def test_m1_gdex_shaped_summary_stays_faithful():
    """GDEX-style single-summary invoice keeps one summary row (not collapsed further)."""
    doc = ExtractedDocument(
        doc_type="invoice",
        page_range=[1, 1],
        vendor="Courier-A",
        buyer="Buyer-B",
        reference="GDBA3838384",
        date="2025-12-15",
        currency="MYR",
        presentation="summary",
        lines=[
            ExtractedDocumentLine(
                description="Courier charges — standard rated",
                net_amount=71.27,
                gst_amount=4.28,
                tax_label="SR",
            ),
        ],
        subtotal=71.27,
        tax_total=4.28,
        grand_total=75.55,
        direction_for_client="purchase",
        tax_visible_on_document=True,
    )
    ok, detail = validate_extracted_document(doc)
    assert ok, detail
    inv = extracted_document_to_normalized(doc, direction="purchase", base_currency="MYR")
    assert len(inv.lines) == 1
    assert inv.doc_total == 75.55
    assert "Courier charges" in inv.lines[0].description


def test_telco_gst_bucket_lines_dedupes_duplicate_buckets():
    record = _telco_bill_a_capture()
    lines = telco_gst_bucket_lines(record)
    assert lines is not None
    assert len(lines) == 2
