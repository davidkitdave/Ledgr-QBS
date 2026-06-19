"""Tests for multi-receipt bundle splitting + FX conversion in the invoice extractor.

All tests are hermetic — no Gemini / network calls.  We build ExtractedInvoice
and ExtractedInvoiceBundle fixtures directly and test the pure-Python logic in
  • to_normalized()  — FX fields populated when currency != base_currency
  • to_normalized_bundle() — splits bundle into N NormalizedInvoices, skips SOA
  • reconcile()       — per-doc reconcile still passes for each split doc

TDD: these tests were written BEFORE the implementation.
"""

from __future__ import annotations

import pytest

from invoice_processing.extract.invoice_extractor import (
    ExtractedInvoice,
    ExtractedInvoiceBundle,
    ExtractedLine,
    _parse_date,
    reconcile,
    to_normalized,
    to_normalized_bundle,
)
from invoice_processing.export.models import NormalizedInvoice


# =========================================================================== #
# Helpers / fixtures
# =========================================================================== #

def _make_receipt(
    *,
    invoice_number: str,
    currency: str,
    total: float,
    subtotal: float | None = None,
    gst_total: float = 0.0,
    description: str = "Test charge",
    net_amount: float | None = None,
    gst_amount: float = 0.0,
    issuer_name: str = "Vendor Pte Ltd",
) -> ExtractedInvoice:
    if net_amount is None:
        net_amount = total - gst_total
    if subtotal is None:
        subtotal = net_amount
    return ExtractedInvoice(
        doc_type="receipt",
        invoice_number=invoice_number,
        invoice_date="2024-03-15",
        currency=currency,
        issuer_name=issuer_name,
        bill_to_name="Test Client Pte Ltd",
        lines=[
            ExtractedLine(
                description=description,
                net_amount=net_amount,
                gst_amount=gst_amount,
                tax_label="NT" if gst_amount == 0 else "SR",
            )
        ],
        subtotal=subtotal,
        gst_total=gst_total,
        total=total,
    )


def _make_bundle(*receipts: ExtractedInvoice, skipped_pages=None, notes=None) -> ExtractedInvoiceBundle:
    return ExtractedInvoiceBundle(
        invoices=list(receipts),
        skipped_pages=skipped_pages,
        notes=notes,
    )


# =========================================================================== #
# A — Bundle splitting: multi-receipt → N NormalizedInvoices
# =========================================================================== #

class TestBundleSplit:
    """A bundled multi-receipt input splits into N NormalizedInvoices, each reconciling."""

    def test_single_receipt_bundle_returns_one(self):
        """Single-entry bundle degrades gracefully to one NormalizedInvoice."""
        receipt = _make_receipt(invoice_number="R-001", currency="SGD", total=100.0, subtotal=100.0)
        bundle = _make_bundle(receipt)
        results = to_normalized_bundle(bundle, direction="purchase", base_currency="SGD")
        assert len(results) == 1
        assert results[0].invoice_number == "R-001"

    def test_two_receipt_bundle_returns_two(self):
        """Two receipts in bundle → two NormalizedInvoices."""
        r1 = _make_receipt(invoice_number="R-001", currency="SGD", total=100.0, subtotal=100.0)
        r2 = _make_receipt(invoice_number="R-002", currency="SGD", total=200.0, subtotal=200.0)
        bundle = _make_bundle(r1, r2)
        results = to_normalized_bundle(bundle, direction="purchase", base_currency="SGD")
        assert len(results) == 2
        nums = {r.invoice_number for r in results}
        assert nums == {"R-001", "R-002"}

    def test_four_receipt_bundle_returns_four(self):
        """Four receipts on one scanned page → four NormalizedInvoices."""
        receipts = [
            _make_receipt(invoice_number=f"R-00{i}", currency="SGD", total=float(i * 10), subtotal=float(i * 10))
            for i in range(1, 5)
        ]
        bundle = _make_bundle(*receipts)
        results = to_normalized_bundle(bundle, direction="purchase", base_currency="SGD")
        assert len(results) == 4

    def test_each_doc_reconciles(self):
        """Each split NormalizedInvoice must individually reconcile (lines == totals)."""
        r1 = _make_receipt(invoice_number="R-001", currency="SGD", total=500.0, subtotal=500.0, net_amount=500.0)
        r2 = _make_receipt(invoice_number="R-002", currency="SGD", total=750.0, subtotal=750.0, net_amount=750.0)
        bundle = _make_bundle(r1, r2)
        results = to_normalized_bundle(bundle, direction="purchase", base_currency="SGD")
        for inv in results:
            assert inv.reconciled, f"Invoice {inv.invoice_number} failed reconcile: {inv.reconcile_note}"

    def test_empty_bundle_returns_empty_list(self):
        """An empty bundle (no invoices) returns an empty list without crashing."""
        bundle = ExtractedInvoiceBundle(invoices=[])
        results = to_normalized_bundle(bundle, direction="purchase", base_currency="SGD")
        assert results == []

    def test_bundle_direction_purchase_sets_doc_type(self):
        """direction='purchase' → each NormalizedInvoice.doc_type == 'purchase'."""
        r = _make_receipt(invoice_number="R-001", currency="SGD", total=100.0, subtotal=100.0)
        bundle = _make_bundle(r)
        results = to_normalized_bundle(bundle, direction="purchase", base_currency="SGD")
        assert results[0].doc_type == "purchase"

    def test_bundle_direction_sales_sets_doc_type(self):
        """direction='sales' → each NormalizedInvoice.doc_type == 'sales'."""
        r = _make_receipt(invoice_number="I-001", currency="SGD", total=100.0, subtotal=100.0)
        bundle = _make_bundle(r)
        results = to_normalized_bundle(bundle, direction="sales", base_currency="SGD")
        assert results[0].doc_type == "sales"


# =========================================================================== #
# B — SOA cover-page skipping
# =========================================================================== #

class TestSoaSkipping:
    """An SOA cover-page-only bundle (no real invoices) produces zero NormalizedInvoices."""

    def test_soa_bundle_with_no_invoices_returns_empty(self):
        """SOA package where only the cover was found → empty list; skipped_pages recorded."""
        bundle = ExtractedInvoiceBundle(
            invoices=[],
            skipped_pages=[1],
            notes="SOA cover page skipped; no embedded invoices found",
        )
        results = to_normalized_bundle(bundle, direction="purchase", base_currency="SGD")
        assert results == []

    def test_soa_bundle_real_invoices_only(self):
        """SOA package: cover skipped (page 1), two embedded invoices extracted."""
        r1 = _make_receipt(invoice_number="INV-2024-001", currency="SGD", total=300.0, subtotal=300.0)
        r2 = _make_receipt(invoice_number="INV-2024-002", currency="SGD", total=450.0, subtotal=450.0)
        bundle = _make_bundle(r1, r2, skipped_pages=[1])
        results = to_normalized_bundle(bundle, direction="purchase", base_currency="SGD")
        assert len(results) == 2


# =========================================================================== #
# C — Currency: record as shown, no conversion
# =========================================================================== #

class TestFxConversion:
    """Ledgr records invoice amounts in the document's currency exactly as printed.
    No FX conversion is ever applied.  The accountant converts in their ERP."""

    def test_sgd_base_currency_no_fx_needed(self):
        """SGD doc with SGD base → fx_rate=None (no rate printed), amounts unchanged,
        not flagged for review."""
        r = _make_receipt(invoice_number="R-SGD", currency="SGD", total=100.0, subtotal=100.0)
        inv = to_normalized(r, direction="purchase", base_currency="SGD")
        # No rate printed → None, not 1.0
        assert inv.fx_rate is None
        assert inv.original_currency is None
        assert inv.original_total is None
        assert not inv.needs_fx_review
        # Currency stays as the document currency
        assert inv.currency == "SGD"
        # Amounts unchanged
        assert inv.doc_total == pytest.approx(100.0)

    def test_usd_doc_with_rate_records_as_shown(self):
        """USD doc with fx_rate=1.35 → currency=USD, amounts UNCHANGED (not multiplied),
        fx_rate=1.35 (the printed rate stored faithfully), not flagged."""
        r = _make_receipt(invoice_number="R-USD", currency="USD", total=1000.0, subtotal=1000.0, net_amount=1000.0)
        inv = to_normalized(r, direction="purchase", base_currency="SGD", fx_rate=1.35)
        # Document currency is recorded, never converted to SGD
        assert inv.currency == "USD"
        # Amounts are the USD values — NOT multiplied by 1.35
        assert inv.doc_total == pytest.approx(1000.0, abs=0.01)
        assert inv.doc_subtotal == pytest.approx(1000.0, abs=0.01)
        # Printed rate stored faithfully
        assert inv.fx_rate == pytest.approx(1.35)
        # No "original" fields — there is no conversion, so there is no before/after
        assert inv.original_currency is None
        assert inv.original_total is None
        # Single foreign currency → not flagged
        assert not inv.needs_fx_review

    def test_idr_doc_with_rate_records_as_shown(self):
        """IDR doc with fx_rate=0.000085 → currency=IDR, amounts UNCHANGED,
        fx_rate=0.000085 (printed rate recorded at full precision), not flagged."""
        r = _make_receipt(invoice_number="R-IDR", currency="IDR", total=974470.0, subtotal=974470.0, net_amount=974470.0)
        inv = to_normalized(r, direction="purchase", base_currency="SGD", fx_rate=0.000085)
        # Document currency recorded as-is
        assert inv.currency == "IDR"
        # Amounts are the IDR values — NOT converted to SGD
        assert inv.doc_total == pytest.approx(974470.0, abs=0.01)
        assert inv.doc_subtotal == pytest.approx(974470.0, abs=0.01)
        # Printed rate stored faithfully
        assert inv.fx_rate == pytest.approx(0.000085)
        assert inv.original_currency is None
        assert inv.original_total is None
        assert not inv.needs_fx_review

    def test_fx_rate_stored_in_export_row(self):
        """QbsLedgerExporter: Currency Rate column carries the printed rate (1.35) when
        present; Currency column is the document currency (USD, not SGD)."""
        from invoice_processing.export.exporters import QbsLedgerExporter
        from invoice_processing.export.models import InvoiceLine, PartyInfo

        inv = NormalizedInvoice(
            doc_type="purchase",
            invoice_number="R-USD",
            currency="USD",
            fx_rate=1.35,
            needs_fx_review=False,
            supplier=PartyInfo(name="Overseas Vendor"),
            lines=[InvoiceLine(description="Consulting", net_amount=1000.0, gst_amount=0.0, tax_treatment="NT")],
            doc_total=1000.0,
        )
        exporter = QbsLedgerExporter()
        rows = exporter.rows([inv], "purchase")
        assert len(rows) == 1
        # Printed rate recorded faithfully
        assert rows[0]["Currency Rate"] == pytest.approx(1.35)
        # Document currency, not base currency
        assert rows[0]["Currency"] == "USD"

    def test_fx_rate_blank_in_export_row_when_none(self):
        """QbsLedgerExporter: Currency Rate is blank ("") when no rate was printed —
        never a silent 1.0."""
        from invoice_processing.export.exporters import QbsLedgerExporter
        from invoice_processing.export.models import InvoiceLine, PartyInfo

        inv = NormalizedInvoice(
            doc_type="purchase",
            invoice_number="R-USD-NORATE",
            currency="USD",
            fx_rate=None,
            needs_fx_review=False,
            supplier=PartyInfo(name="Overseas Vendor"),
            lines=[InvoiceLine(description="Consulting", net_amount=500.0, gst_amount=0.0, tax_treatment="NT")],
            doc_total=500.0,
        )
        exporter = QbsLedgerExporter()
        rows = exporter.rows([inv], "purchase")
        assert len(rows) == 1
        assert rows[0]["Currency Rate"] == ""
        assert rows[0]["Currency"] == "USD"

    def test_fx_rate_in_sales_row(self):
        """QbsLedgerExporter sales row: printed rate stored, document currency used."""
        from invoice_processing.export.exporters import QbsLedgerExporter
        from invoice_processing.export.models import InvoiceLine, PartyInfo

        inv = NormalizedInvoice(
            doc_type="sales",
            invoice_number="S-USD",
            currency="USD",
            fx_rate=1.35,
            needs_fx_review=False,
            customer=PartyInfo(name="Overseas Customer"),
            lines=[InvoiceLine(description="Services", net_amount=500.0, gst_amount=0.0, tax_treatment="NT")],
            doc_total=500.0,
        )
        exporter = QbsLedgerExporter()
        rows = exporter.rows([inv], "sales")
        assert len(rows) == 1
        assert rows[0]["Currency Rate"] == pytest.approx(1.35)
        assert rows[0]["Currency"] == "USD"


# =========================================================================== #
# D — FX: no derivable rate → flag for review, NOT stored at rate=1
# =========================================================================== #

class TestFxNoRate:
    """Single-currency foreign docs book in document currency; mixed-currency docs flag."""

    def test_usd_doc_no_rate_books_in_usd(self):
        """USD doc with no fx_rate → booked in USD, not flagged."""
        r = _make_receipt(invoice_number="R-USD-NORATE", currency="USD", total=500.0, subtotal=500.0)
        inv = to_normalized(r, direction="purchase", base_currency="SGD")
        assert inv.needs_fx_review is False
        assert inv.reconciled is True
        assert inv.currency == "USD"
        assert inv.fx_rate is None

    def test_usd_doc_no_rate_not_converted_to_sgd(self):
        """USD doc with no fx_rate → amounts stay in USD (not silently converted)."""
        r = _make_receipt(invoice_number="R-USD-NORATE", currency="USD", total=500.0, subtotal=500.0)
        inv = to_normalized(r, direction="purchase", base_currency="SGD")
        assert inv.doc_total == 500.0
        assert inv.currency == "USD"

    def test_idr_doc_no_rate_books_in_idr(self):
        """IDR doc with no fx_rate → booked in IDR, not flagged."""
        r = _make_receipt(invoice_number="R-IDR-NORATE", currency="IDR", total=974470.0, subtotal=974470.0)
        inv = to_normalized(r, direction="purchase", base_currency="SGD")
        assert inv.needs_fx_review is False
        assert inv.currency == "IDR"

    def test_mixed_currency_doc_is_flagged(self):
        """Same document with IDR lines and USD header → needs_fx_review."""
        r = _make_receipt(invoice_number="R-MIX", currency="USD", total=311.79, subtotal=311.79)
        inv = to_normalized(
            r,
            direction="purchase",
            base_currency="SGD",
            currency_conflict=True,
            line_currencies=["IDR", "IDR", "USD"],
        )
        assert inv.needs_fx_review is True
        assert inv.reconciled is False
        assert "multiple currencies" in (inv.reconcile_note or "").lower()

    def test_sgd_doc_no_rate_not_flagged(self):
        """SGD (= base currency) doc with no fx_rate → NOT flagged (no FX needed)."""
        r = _make_receipt(invoice_number="R-SGD", currency="SGD", total=100.0, subtotal=100.0)
        inv = to_normalized(r, direction="purchase", base_currency="SGD")
        assert inv.needs_fx_review is False

    def test_bundle_with_mixed_currencies_no_rate_not_flagged(self):
        """Separate single-currency docs in a bundle are not flagged."""
        r_sgd = _make_receipt(invoice_number="R-SGD", currency="SGD", total=100.0, subtotal=100.0)
        r_usd = _make_receipt(invoice_number="R-USD", currency="USD", total=50.0, subtotal=50.0)
        r_idr = _make_receipt(invoice_number="R-IDR", currency="IDR", total=50000.0, subtotal=50000.0)
        bundle = _make_bundle(r_sgd, r_usd, r_idr)
        results = to_normalized_bundle(bundle, direction="purchase", base_currency="SGD")
        assert len(results) == 3
        by_num = {r.invoice_number: r for r in results}
        assert by_num["R-SGD"].needs_fx_review is False
        assert by_num["R-USD"].needs_fx_review is False
        assert by_num["R-IDR"].needs_fx_review is False


# =========================================================================== #
# E — Reconcile still works per-doc after split
# =========================================================================== #

class TestReconcilePerDoc:
    """reconcile() on individual ExtractedInvoices still passes after bundle logic."""

    def test_reconcile_passes_matching_totals(self):
        r = _make_receipt(invoice_number="R-001", currency="SGD", total=100.0, subtotal=100.0, net_amount=100.0)
        ok, detail = reconcile(r)
        assert ok is True

    def test_reconcile_fails_mismatched_totals(self):
        """Lines sum to 974470 but doc total says 605500 → reconcile fails."""
        r = ExtractedInvoice(
            doc_type="receipt",
            invoice_number="R-FAIL",
            currency="IDR",
            lines=[
                ExtractedLine(description="Charge A", net_amount=500000.0, gst_amount=0.0),
                ExtractedLine(description="Charge B", net_amount=474470.0, gst_amount=0.0),
            ],
            subtotal=974470.0,
            gst_total=0.0,
            total=605500.0,  # wrong — causes the reconcile failure from the task description
        )
        ok, detail = reconcile(r)
        assert ok is False
        assert "total" in detail.lower()

    def test_two_receipts_reconcile_independently(self):
        """Two receipts with different totals each reconcile independently."""
        r1 = _make_receipt(invoice_number="R-001", currency="SGD", total=100.0, subtotal=100.0, net_amount=100.0)
        r2 = _make_receipt(invoice_number="R-002", currency="IDR", total=50000.0, subtotal=50000.0, net_amount=50000.0)
        ok1, _ = reconcile(r1)
        ok2, _ = reconcile(r2)
        assert ok1 is True
        assert ok2 is True


# =========================================================================== #
# G — Date parsing for ledger export
# =========================================================================== #

class TestParseDate:
    def test_text_month_year(self):
        assert str(_parse_date("15 Jan 2025")) == "2025-01-15"

    def test_ordinal_date_range_uses_end(self):
        assert str(_parse_date("17th November 2025 - 19th November 2025")) == "2025-11-19"

    def test_iso_date(self):
        assert str(_parse_date("2025-12-01")) == "2025-12-01"

    def test_text_month_via_to_normalized(self):
        r = _make_receipt(
            invoice_number="R-DATE",
            currency="USD",
            total=100.0,
            subtotal=100.0,
        )
        r.invoice_date = "15 Jan 2025"
        inv = to_normalized(r, direction="purchase", base_currency="SGD")
        assert str(inv.invoice_date) == "2025-01-15"


# =========================================================================== #
# F — NormalizedInvoice has the new FX fields
# =========================================================================== #

class TestNormalizedInvoiceFxFields:
    """NormalizedInvoice dataclass exposes fx_rate, original_total, original_currency, needs_fx_review."""

    def test_fx_fields_exist_on_model(self):
        from invoice_processing.export.models import NormalizedInvoice
        inv = NormalizedInvoice()
        assert hasattr(inv, "fx_rate")
        assert hasattr(inv, "original_total")
        assert hasattr(inv, "original_currency")
        assert hasattr(inv, "needs_fx_review")

    def test_fx_fields_default_values(self):
        from invoice_processing.export.models import NormalizedInvoice
        inv = NormalizedInvoice()
        # Defaults: no rate printed (None, never 1.0), no review needed
        assert inv.fx_rate is None
        assert inv.original_total is None
        assert inv.original_currency is None
        assert inv.needs_fx_review is False
