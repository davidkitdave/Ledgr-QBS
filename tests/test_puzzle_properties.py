"""Simple Intelligent Puzzle — property tests (ADR-0014).

Behaviors apply to any document shape, not vendor-specific filenames.
"""

from __future__ import annotations

from datetime import date

import pytest

from invoice_processing.export.exporters import XeroLedgerExporter
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from invoice_processing.export.tax_classifier import TaxClassifier, classify_invoice
from invoice_processing.extract.invoice_extractor import ExtractedInvoice, ExtractedLine, reconcile
from accounting_agents.nodes import _needs_review

pytestmark = pytest.mark.unit


def _no_tax_expense_invoice(*, doc_type: str = "purchase") -> NormalizedInvoice:
    """Three lines, no GST on document — EXP25-shaped totals without invented tax."""
    supplier = PartyInfo(name="Employee Claimant", country="SG")
    return NormalizedInvoice(
        doc_type=doc_type,
        invoice_number="REF-001",
        invoice_date=date(2025, 3, 16),
        currency="USD",
        supplier=supplier if doc_type == "purchase" else PartyInfo(),
        customer=PartyInfo() if doc_type == "purchase" else supplier,
        our_gst_registered=True,
        tax_visible_on_document=False,
        doc_total=1195.11,
        lines=[
            InvoiceLine(description="Transport", net_amount=263.85, gst_amount=0.0),
            InvoiceLine(description="Accommodation", net_amount=274.14, gst_amount=0.0),
            InvoiceLine(description="Other", net_amount=457.12, gst_amount=0.0),
        ],
    )


class TestTaxNotInvented:
    def test_export_never_invents_gst_when_document_has_no_tax_column(self):
        inv = _no_tax_expense_invoice()
        classify_invoice(inv)
        rows = XeroLedgerExporter().rows([inv], inv.doc_type)
        assert len(rows) == 3
        for row in rows:
            assert row["TaxAmount"] == 0.0
            assert row["*TaxType"] == "No Tax"

    def test_sr_line_without_gst_amount_exports_zero_tax(self):
        inv = NormalizedInvoice(
            doc_type="purchase",
            invoice_number="INV-1",
            invoice_date=date(2025, 1, 1),
            our_gst_registered=True,
            tax_visible_on_document=True,
            supplier=PartyInfo(name="Vendor", gst_regno="200012345A"),
            lines=[
                InvoiceLine(
                    description="Services",
                    net_amount=100.0,
                    gst_amount=None,
                    tax_keyword="SR",
                ),
            ],
        )
        classify_invoice(inv)
        row = XeroLedgerExporter().rows([inv], "purchase")[0]
        assert row["TaxAmount"] == 0.0


class TestTaxClassifierNoTaxDocument:
    def test_purchase_nt_high_confidence_when_no_tax_visible(self):
        inv = _no_tax_expense_invoice()
        classify_invoice(inv)
        for line in inv.lines:
            assert line.tax_treatment == "NT"
            assert line.tax_confidence >= 0.9
            assert line.tax_flagged is False

    def test_sales_defaults_nt_when_no_tax_visible_not_sr(self):
        inv = _no_tax_expense_invoice(doc_type="sales")
        classify_invoice(inv)
        for line in inv.lines:
            assert line.tax_treatment == "NT"
            assert line.tax_flagged is False


class TestApprovalNotNoisyForExpectedNt:
    def test_expected_nt_not_approval_blocked(self):
        inv = _no_tax_expense_invoice()
        classify_invoice(inv)
        state = {
            "normalized_invoices": [
                {
                    "doc_type": inv.doc_type,
                    "invoice_number": inv.invoice_number,
                    "reconciled": True,
                    "lines": [
                        {
                            "description": line.description,
                            "tax_flagged": line.tax_flagged,
                            "tax_confidence": line.tax_confidence,
                            "tax_reason": line.tax_reason,
                        }
                        for line in inv.lines
                    ],
                }
            ],
        }
        needs, reasons = _needs_review(state)
        assert needs is False
        assert reasons == []


class TestFooterReconcile:
    def test_footer_reconcile_passes_when_lines_match_total(self):
        ex = ExtractedInvoice(
            doc_type="invoice",
            invoice_number="X",
            lines=[
                ExtractedLine(description="a", net_amount=263.85),
                ExtractedLine(description="b", net_amount=274.14),
                ExtractedLine(description="c", net_amount=457.12),
            ],
            subtotal=757.85,
            total=995.11,
        )
        ok, note = reconcile(
            ex,
            tax_visible_on_document=False,
            subtotal_in_capture=False,
        )
        assert ok is True
        assert "ok" in note.lower()

    def test_subtotal_check_skipped_when_not_in_capture(self):
        ex = ExtractedInvoice(
            doc_type="invoice",
            invoice_number="X",
            lines=[
                ExtractedLine(description="a", net_amount=168.8),
                ExtractedLine(description="b", net_amount=654.25),
            ],
            subtotal=757.85,
            total=823.05,
        )
        ok, _ = reconcile(
            ex,
            tax_visible_on_document=False,
            subtotal_in_capture=False,
        )
        assert ok is True

    def test_gst_check_skipped_when_no_tax_on_document(self):
        ex = ExtractedInvoice(
            doc_type="invoice",
            invoice_number="X",
            lines=[ExtractedLine(description="a", net_amount=100.0, gst_amount=0.0)],
            gst_total=9.0,
            total=100.0,
        )
        ok, _ = reconcile(ex, tax_visible_on_document=False, subtotal_in_capture=False)
        assert ok is True
