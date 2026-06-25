"""Tests for tax-inclusive (MY SST / SG GST) reconcile guard (issue #14).

When tax is shown only in the footer (no per-line tax column), extraction correctly
sets line gst_amount=0 and tax_visible_on_document=True. reconcile() must not
hard-review on gst/subtotal mismatches — validate on grand_total only.

TDD: written before the reconcile() implementation (red → green).
"""

from __future__ import annotations

import pytest

from invoice_processing.extract.invoice_extractor import (
    ExtractedInvoice,
    ExtractedLine,
    reconcile,
    to_normalized,
)
from invoice_processing.extract.ledger_extract import (
    ExtractedDocument,
    ExtractedDocumentLine,
    validate_extracted_document,
)


def _my_sst_tax_inclusive_invoice(*, grand_total: float = 65.0) -> ExtractedInvoice:
    """MY SST invoice: line nets are gross; footer splits ex-tax + SST."""
    return ExtractedInvoice(
        doc_type="invoice",
        invoice_number="MY-SST-001",
        invoice_date="2026-06-01",
        currency="MYR",
        issuer_country="MY",
        lines=[
            ExtractedLine(description="Widget A", net_amount=40.0, gst_amount=0.0),
            ExtractedLine(description="Widget B", net_amount=25.0, gst_amount=0.0),
        ],
        subtotal=60.19,
        gst_total=4.81,
        total=grand_total,
        issuer_tax_system="SST",
    )


class TestTaxInclusiveReconcile:
    """Tracer bullet + acceptance cases for issue #14."""

    def test_my_sst_tax_inclusive_reconciles_on_grand_total(self):
        """Line nets sum to gross; per-line gst=0 must not trip gst/subtotal checks."""
        ex = _my_sst_tax_inclusive_invoice()
        net_sum = sum(ln.net_amount or 0.0 for ln in ex.lines)
        assert net_sum == pytest.approx(ex.total or 0.0)
        assert sum(ln.gst_amount or 0.0 for ln in ex.lines) == 0.0

        ok, detail = reconcile(ex, tax_visible_on_document=True, currency="MYR")
        assert ok is True, detail
        assert "gst" not in detail.lower()
        assert "subtotal" not in detail.lower()

    def test_sg_gst_tax_inclusive_reconciles_on_grand_total(self):
        ex = ExtractedInvoice(
            doc_type="invoice",
            invoice_number="SG-GST-001",
            currency="SGD",
            issuer_tax_system="GST",
            lines=[
                ExtractedLine(description="Consulting", net_amount=109.0, gst_amount=0.0),
            ],
            subtotal=100.0,
            gst_total=9.0,
            total=109.0,
        )
        ok, detail = reconcile(ex, tax_visible_on_document=True, currency="SGD")
        assert ok is True, detail

    def test_wrong_grand_total_still_fails(self):
        ex = _my_sst_tax_inclusive_invoice(grand_total=70.0)
        ok, detail = reconcile(ex, tax_visible_on_document=True, currency="MYR")
        assert ok is False
        assert "total" in detail.lower()

    def test_tax_exclusive_per_line_gst_unchanged(self):
        """Per-line gst column — must still reconcile subtotal + gst normally."""
        ex = ExtractedInvoice(
            doc_type="invoice",
            invoice_number="TELCO-001",
            currency="SGD",
            issuer_tax_system="GST",
            lines=[
                ExtractedLine(description="SR line", net_amount=1164.42, gst_amount=104.80),
                ExtractedLine(description="ZR line", net_amount=58.93, gst_amount=0.0),
            ],
            subtotal=1223.35,
            gst_total=104.80,
            total=1328.15,
        )
        ok, detail = reconcile(ex, tax_visible_on_document=True, currency="SGD")
        assert ok is True, detail

    def test_lines_sum_ex_tax_not_tax_inclusive_pattern(self):
        """H2 probe: nets sum to ex-tax subtotal — not footer-inclusive; gst check fails."""
        ex = ExtractedInvoice(
            doc_type="invoice",
            invoice_number="H2-001",
            currency="MYR",
            issuer_tax_system="SST",
            lines=[
                ExtractedLine(description="Item", net_amount=60.19, gst_amount=0.0),
            ],
            subtotal=60.19,
            gst_total=4.81,
            total=65.0,
        )
        ok, detail = reconcile(ex, tax_visible_on_document=True, currency="MYR")
        assert ok is False
        assert "gst" in detail.lower()

    def test_validate_extracted_document_tax_inclusive_path(self):
        doc = ExtractedDocument(
            doc_type="invoice",
            page_range=[1, 1],
            vendor="MY Vendor",
            reference="MY-SST-001",
            date="2026-06-01",
            currency="MYR",
            subtotal=60.19,
            tax_total=4.81,
            grand_total=65.0,
            tax_visible_on_document=True,
            lines=[
                ExtractedDocumentLine(description="Widget A", net_amount=40.0, gst_amount=0.0),
                ExtractedDocumentLine(description="Widget B", net_amount=25.0, gst_amount=0.0),
            ],
        )
        ok, detail = validate_extracted_document(doc)
        assert ok is True, detail

    def test_to_normalized_tax_inclusive_is_reconciled(self):
        ex = _my_sst_tax_inclusive_invoice()
        inv = to_normalized(ex, direction="purchase", base_currency="MYR")
        inv.tax_visible_on_document = True
        assert inv.reconciled is True
