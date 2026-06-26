"""Tests for Drive-parity ledger extract mapper (no LLM)."""

from __future__ import annotations

from invoice_processing.export.client_context import EntityMemoryEntry
from invoice_processing.extract.ledger_extract import (
    FAITHFUL_EXTRACT_STATIC_INSTRUCTION,
    ExtractedDocument,
    ExtractedDocumentBundle,
    ExtractedDocumentLine,
    extracted_document_to_extracted_invoice,
    extracted_document_to_normalized,
    use_understand_extract,
    validate_extracted_document,
)

def test_use_understand_extract_defaults_on(monkeypatch):
    monkeypatch.delenv("LEDGR_UNDERSTAND_EXTRACT", raising=False)
    assert use_understand_extract() is True
    monkeypatch.setenv("LEDGR_UNDERSTAND_EXTRACT", "0")
    assert use_understand_extract() is False


def test_tax_visible_propagates_to_normalized():
    doc = ExtractedDocument(
        doc_type="invoice",
        page_range=[1, 1],
        vendor="Acme Vendor",
        reference="INV-1",
        date="2026-06-17",
        currency="SGD",
        grand_total=100.0,
        subtotal=100.0,
        tax_total=0.0,
        tax_visible_on_document=False,
        lines=[
            ExtractedDocumentLine(description="Service", net_amount=100.0, gst_amount=0.0),
        ],
    )
    inv = extracted_document_to_normalized(doc, direction="purchase")
    assert inv.tax_visible_on_document is False


def test_mapper_sample_test_group_shape():
    doc = ExtractedDocument(
        doc_type="invoice",
        page_range=[1, 1],
        vendor="Sample Vendor Pte Ltd",
        buyer="Company-A",
        reference="2026/0210",
        date="2026-05-01",
        currency="SGD",
        grand_total=1500.0,
        subtotal=1500.0,
        tax_total=0.0,
        lines=[
            ExtractedDocumentLine(
                description="Management fees",
                net_amount=1500.0,
                gst_amount=0.0,
                tax_label="NT",
            ),
        ],
    )
    ex = extracted_document_to_extracted_invoice(doc)
    assert ex.invoice_number == "2026/0210"
    assert len(ex.lines) == 1
    ok, _ = validate_extracted_document(doc)
    assert ok

    inv = extracted_document_to_normalized(
        doc, direction="purchase", our_gst_registered=False
    )
    assert inv.invoice_number == "2026/0210"
    assert len(inv.lines) == 1
    assert inv.reconciled


def test_mapper_telco_two_lines():
    doc = ExtractedDocument(
        doc_type="invoice",
        page_range=[1, 1],
        vendor="Telco Provider A Ltd",
        reference="8004483920122025",
        date="2025-12-04",
        currency="SGD",
        grand_total=1328.15,
        subtotal=1223.35,
        tax_total=104.80,
        lines=[
            ExtractedDocumentLine(
                description="Telecommunication services - standard rated (9%)",
                net_amount=1164.42,
                gst_amount=104.80,
                tax_label="SR",
            ),
            ExtractedDocumentLine(
                description="Telecommunication services - zero rated",
                net_amount=58.93,
                gst_amount=0.0,
                tax_label="ZR",
            ),
        ],
    )
    ok, detail = validate_extracted_document(doc)
    assert ok, detail
    inv = extracted_document_to_normalized(
        doc, direction="purchase", our_gst_registered=False
    )
    assert len(inv.lines) == 2
    assert inv.doc_total == 1328.15


def test_expense_claim_claimant_becomes_issuer():
    """For expense_claim, claimant_name wins over vendor as issuer.

    This is the data-shape plumbing the prompt teaches the model — no Python
    rule switch downstream (ADR-0015). Round-trips through the mapper without
    flipping direction.
    """
    doc = ExtractedDocument(
        doc_type="expense_claim",
        page_range=[1, 1],
        vendor="Company A Pte Ltd",
        buyer="Company A Pte Ltd",
        reference="EXP-2026-001",
        date="2026-03-16",
        currency="SGD",
        grand_total=1195.11,
        subtotal=1195.11,
        tax_total=0.0,
        claimant_name="Person 1",
        tax_visible_on_document=False,
        direction_for_client="purchase",
        direction_reason="claimant signed the form; client is the approver",
        lines=[
            ExtractedDocumentLine(description="Transportation", net_amount=263.85, gst_amount=0.0),
            ExtractedDocumentLine(description="Accommodations", net_amount=274.14, gst_amount=0.0),
            ExtractedDocumentLine(description="Travel per diem", net_amount=600.00, gst_amount=0.0),
            ExtractedDocumentLine(description="Taxi Fee Yangon", net_amount=57.12, gst_amount=0.0),
        ],
    )
    ex = extracted_document_to_extracted_invoice(doc)
    # Claimant wins over letterhead for the supplier/issuer slot.
    assert ex.issuer_name == "Person 1"
    assert ex.doc_type == "invoice"
    assert ex.total == 1195.11
    inv = extracted_document_to_normalized(
        doc, direction="purchase", our_gst_registered=False
    )
    assert inv.tax_visible_on_document is False
    # Non-GST-registered client + tax_visible=False -> every line NT.
    from invoice_processing.export.tax_classifier import classify_invoice
    classify_invoice(inv)
    for line in inv.lines:
        assert line.tax_treatment == "NT"


def test_doc_type_defaults_and_claimant_none():
    """Optional fields default when omitted."""
    doc = ExtractedDocument(
        doc_type="invoice",
        page_range=[1, 1],
        vendor="Acme Vendor",
        reference="INV-1",
        date="2026-06-17",
        currency="SGD",
        grand_total=100.0,
        subtotal=100.0,
        tax_total=0.0,
        lines=[ExtractedDocumentLine(description="Service", net_amount=100.0, gst_amount=0.0)],
    )
    assert doc.doc_type == "invoice"
    assert doc.claimant_name is None
    assert doc.direction_reason is None
    assert doc.tax_visible_on_document is False


def test_currency_propagates_to_normalized_and_exporter():
    """A USD expense claim must stay USD through the pipeline (not silently SGD).

    Reproduces the screenshot bug: extraction's ``currency`` field must
    reach the Xero exporter's ``Currency`` column unchanged, even when
    the client's base_currency is SGD. This is the plumbing test for
    the F-cluster ``currency_routing_score`` gate.
    """
    doc = ExtractedDocument(
        doc_type="expense_claim",
        page_range=[1, 1],
        vendor="Person-1",
        reference="EXP-1",
        date="2026-06-17",
        currency="USD",
        grand_total=1195.11,
        subtotal=1195.11,
        tax_total=0.0,
        claimant_name="Person-1",
        tax_visible_on_document=False,
        direction_for_client="purchase",
        direction_reason="claimant signed the form; client is the approver",
        lines=[
            ExtractedDocumentLine(description="Transportation", net_amount=131.77, gst_amount=0.0),
            ExtractedDocumentLine(description="Accommodations", net_amount=274.14, gst_amount=0.0),
        ],
    )
    inv = extracted_document_to_normalized(
        doc,
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


def test_faithful_extract_static_instruction_soa_and_skipped_pages():
    """SOA cover handling lives in the cacheable static instruction (WS-6.2)."""
    text = FAITHFUL_EXTRACT_STATIC_INSTRUCTION
    assert "skipped_pages" in text
    assert "SOA" in text
    assert "documents[]" in text or "documents``" in text or "documents" in text
    assert "cover" in text.lower()
    assert "NOT a bookable" in text or "not bookable" in text.lower()


def test_faithful_extract_static_instruction_line_granularity():
    """Line rows must not be collapsed during extraction."""
    text = FAITHFUL_EXTRACT_STATIC_INSTRUCTION.lower()
    assert "collapse" in text
    assert "itemized" in text
    assert "presentation" in text
    assert "one" in text and "lines" in text


def test_faithful_extract_static_instruction_direction_abstention():
    """Direction must abstain with unknown — never guess."""
    text = FAITHFUL_EXTRACT_STATIC_INSTRUCTION
    assert "unknown" in text
    assert "never guess" in text.lower() or "Abstain" in text


def test_extracted_document_bundle_schema_descriptions():
    """Field descriptions carry SOA / abstention / granularity rules for Gemini."""
    schema = ExtractedDocumentBundle.model_json_schema()
    props = schema["properties"]
    assert "SOA" in props["skipped_pages"]["description"]
    assert "cover" in props["skipped_pages"]["description"].lower()
    assert "bookable" in props["documents"]["description"].lower()
    assert "uncertainty" in props["notes"]["description"].lower()

    doc_schema = schema["$defs"]["ExtractedDocument"]
    doc_props = doc_schema["properties"]
    assert "statement" in doc_props["doc_type"]["description"]
    assert "skipped_pages" in doc_props["doc_type"]["description"]
    assert "collapse" in doc_props["presentation"]["description"].lower()
    assert "unknown" in doc_props["direction_for_client"]["description"]
    assert "collapse" in doc_props["lines"]["description"].lower()


def test_extracted_document_to_normalized_applies_vendor_role_floor():
    doc = ExtractedDocument(
        doc_type="receipt",
        page_range=[1, 1],
        vendor="NTUC FairPrice",
        reference="RCP-1",
        date="2026-06-01",
        currency="SGD",
        grand_total=10.0,
        subtotal=10.0,
        tax_total=0.0,
        tax_visible_on_document=False,
        direction_for_client="unknown",
        lines=[
            ExtractedDocumentLine(description="Groceries", net_amount=10.0, gst_amount=0.0),
        ],
    )
    memory = [EntityMemoryEntry(name="NTUC FairPrice", role="Creditor", mapping_code="6100")]
    inv = extracted_document_to_normalized(
        doc,
        direction="auto",
        entity_memory=memory,
    )
    assert inv.doc_type == "purchase"
    assert inv.reconciled is True
    assert "direction unknown" not in (inv.reconcile_note or "").lower()

