"""Regression tests for credit-note sign flip at the exporter-row level (issue #32).

These tests lock the sign behaviour produced by ``_doc_sign`` as it propagates
through ``_line_net_amount`` / ``_tax_amount`` / ``_invoice_total`` into the
exported rows of QbsLedgerExporter and XeroLedgerExporter.

The existing tests in test_invoice_extractor_bundle.py::TestCreditNoteSignFlip
stop at the QBS/Xero happy-path.  This file adds:
  * receipt and expense_claim → amounts stay POSITIVE (no flip)
  * causal guard: monkeypatching ``_doc_sign`` to always return +1 makes the
    credit-note row no longer negative, proving that reverting the flip breaks
    this test suite.
"""

from __future__ import annotations

from datetime import date

import pytest

from ledgr_slack.export.exporters import QbsLedgerExporter, XeroLedgerExporter
from ledgr_slack.export.models import InvoiceLine, NormalizedInvoice, PartyInfo


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _credit_note_inv(doc_type: str = "purchase") -> NormalizedInvoice:
    """Minimal credit note with net=100, gst=9, total=109 (fictional data)."""
    party_kwargs = (
        {"supplier": PartyInfo(name="Fictional Supplier Pte Ltd")}
        if doc_type == "purchase"
        else {"customer": PartyInfo(name="Fictional Customer Sdn Bhd")}
    )
    return NormalizedInvoice(
        doc_type=doc_type,
        document_kind="credit_note",
        invoice_number="CN-TEST-001",
        invoice_date=date(2025, 3, 15),
        currency="SGD",
        our_gst_registered=True,
        tax_visible_on_document=True,
        lines=[
            InvoiceLine(
                description="Fictional returned item",
                net_amount=100.0,
                gst_amount=9.0,
                tax_treatment="SR",
            )
        ],
        doc_subtotal=100.0,
        doc_gst_total=9.0,
        doc_total=109.0,
        **party_kwargs,
    )


def _non_credit_inv(document_kind: str, doc_type: str = "purchase") -> NormalizedInvoice:
    """Minimal invoice for receipt / expense_claim / invoice kinds with net=200, gst=18."""
    party_kwargs = (
        {"supplier": PartyInfo(name="Fictional Supplier Pte Ltd")}
        if doc_type == "purchase"
        else {"customer": PartyInfo(name="Fictional Customer Sdn Bhd")}
    )
    return NormalizedInvoice(
        doc_type=doc_type,
        document_kind=document_kind,
        invoice_number="INV-TEST-002",
        invoice_date=date(2025, 3, 15),
        currency="SGD",
        our_gst_registered=True,
        tax_visible_on_document=True,
        lines=[
            InvoiceLine(
                description="Fictional service charge",
                net_amount=200.0,
                gst_amount=18.0,
                tax_treatment="SR",
            )
        ],
        doc_subtotal=200.0,
        doc_gst_total=18.0,
        doc_total=218.0,
        **party_kwargs,
    )


# ---------------------------------------------------------------------------
# QBS — credit note: line net, line tax, invoice total must be negative
# ---------------------------------------------------------------------------

class TestQbsCreditNoteSignExporterRow:
    """Lock the QBS exporter row sign for credit notes."""

    def test_qbs_purchase_credit_note_line_net_negative(self):
        exporter = QbsLedgerExporter()
        rows = exporter.rows([_credit_note_inv("purchase")], "purchase")
        assert len(rows) == 1
        assert rows[0]["Sub Total"] == pytest.approx(-100.0)

    def test_qbs_purchase_credit_note_line_tax_negative(self):
        exporter = QbsLedgerExporter()
        rows = exporter.rows([_credit_note_inv("purchase")], "purchase")
        assert rows[0]["Tax Amount"] == pytest.approx(-9.0)

    def test_qbs_purchase_credit_note_invoice_total_negative(self):
        exporter = QbsLedgerExporter()
        rows = exporter.rows([_credit_note_inv("purchase")], "purchase")
        assert rows[0]["Total Amount"] == pytest.approx(-109.0)

    def test_qbs_sales_credit_note_line_net_negative(self):
        exporter = QbsLedgerExporter()
        rows = exporter.rows([_credit_note_inv("sales")], "sales")
        assert rows[0]["Amount"] == pytest.approx(-100.0)

    def test_qbs_sales_credit_note_line_tax_negative(self):
        exporter = QbsLedgerExporter()
        rows = exporter.rows([_credit_note_inv("sales")], "sales")
        assert rows[0]["Tax Amount"] == pytest.approx(-9.0)

    def test_qbs_sales_credit_note_invoice_total_negative(self):
        exporter = QbsLedgerExporter()
        rows = exporter.rows([_credit_note_inv("sales")], "sales")
        assert rows[0]["Total"] == pytest.approx(-109.0)


# ---------------------------------------------------------------------------
# Xero — credit note: *UnitAmount, TaxAmount, Total must be negative
# ---------------------------------------------------------------------------

class TestXeroCreditNoteSignExporterRow:
    """Lock the Xero exporter row sign for credit notes."""

    def test_xero_purchase_credit_note_unit_amount_negative(self):
        exporter = XeroLedgerExporter()
        rows = exporter.rows([_credit_note_inv("purchase")], "purchase")
        assert len(rows) == 1
        assert rows[0]["*UnitAmount"] == pytest.approx(-100.0)

    def test_xero_purchase_credit_note_tax_amount_negative(self):
        exporter = XeroLedgerExporter()
        rows = exporter.rows([_credit_note_inv("purchase")], "purchase")
        assert rows[0]["TaxAmount"] == pytest.approx(-9.0)

    def test_xero_purchase_credit_note_total_negative(self):
        exporter = XeroLedgerExporter()
        rows = exporter.rows([_credit_note_inv("purchase")], "purchase")
        assert rows[0]["Total"] == pytest.approx(-109.0)


# ---------------------------------------------------------------------------
# Non-credit-note kinds: receipt, expense_claim, invoice → amounts POSITIVE
# ---------------------------------------------------------------------------

class TestNonCreditNoteKindsStayPositive:
    """receipt, expense_claim, invoice must NOT be sign-flipped."""

    @pytest.mark.parametrize("document_kind", ["receipt", "expense_claim", "invoice"])
    def test_qbs_purchase_non_credit_note_line_net_positive(self, document_kind):
        exporter = QbsLedgerExporter()
        rows = exporter.rows([_non_credit_inv(document_kind, "purchase")], "purchase")
        assert len(rows) == 1
        assert rows[0]["Sub Total"] == pytest.approx(200.0), (
            f"document_kind={document_kind!r} Sub Total must be positive"
        )

    @pytest.mark.parametrize("document_kind", ["receipt", "expense_claim", "invoice"])
    def test_qbs_purchase_non_credit_note_tax_positive(self, document_kind):
        exporter = QbsLedgerExporter()
        rows = exporter.rows([_non_credit_inv(document_kind, "purchase")], "purchase")
        assert rows[0]["Tax Amount"] == pytest.approx(18.0), (
            f"document_kind={document_kind!r} Tax Amount must be positive"
        )

    @pytest.mark.parametrize("document_kind", ["receipt", "expense_claim", "invoice"])
    def test_qbs_purchase_non_credit_note_total_positive(self, document_kind):
        exporter = QbsLedgerExporter()
        rows = exporter.rows([_non_credit_inv(document_kind, "purchase")], "purchase")
        assert rows[0]["Total Amount"] == pytest.approx(218.0), (
            f"document_kind={document_kind!r} Total Amount must be positive"
        )


# ---------------------------------------------------------------------------
# Causal guard: _doc_sign forced to +1 → credit-note row is no longer negative
# ---------------------------------------------------------------------------

class TestDocSignCausalGuard:
    """Prove that reverting _doc_sign to always +1 breaks the sign tests.

    The test patches ``ledgr_slack.export.exporters._doc_sign`` to a
    lambda that always returns +1 (as if the flip were removed) and then
    asserts that the QBS credit-note row is NOT negative.  If this test ever
    starts failing it means the credit-note row is negative even when _doc_sign
    returns +1 — which would indicate the sign is being applied elsewhere and
    the causal relationship has changed.
    """

    def test_with_doc_sign_always_positive_credit_note_row_is_not_negative(
        self, monkeypatch
    ):
        """Causal guard: when _doc_sign → +1 the credit-note amounts are positive.

        This is the proof that reverting the flip causes the sign tests above
        to fail: the credit note row flips sign only because _doc_sign returns -1.
        If _doc_sign is reverted to +1, the row is no longer negative — and the
        tests in TestQbsCreditNoteSignExporterRow would then fail.
        """
        import ledgr_slack.export.exporters as _exp

        monkeypatch.setattr(_exp, "_doc_sign", lambda inv: 1)

        exporter = QbsLedgerExporter()
        rows = exporter.rows([_credit_note_inv("purchase")], "purchase")
        row = rows[0]

        # With the flip removed, all amounts are POSITIVE — this is wrong for a
        # credit note but proves the causal dependency on _doc_sign.
        assert row["Sub Total"] > 0, (
            "Causal guard: with _doc_sign=+1, Sub Total is positive (flip removed)"
        )
        assert row["Tax Amount"] > 0, (
            "Causal guard: with _doc_sign=+1, Tax Amount is positive (flip removed)"
        )
        assert row["Total Amount"] > 0, (
            "Causal guard: with _doc_sign=+1, Total Amount is positive (flip removed)"
        )

    def test_causal_guard_xero_with_doc_sign_always_positive(self, monkeypatch):
        """Causal guard (Xero): _doc_sign→+1 makes *UnitAmount positive for credit note."""
        import ledgr_slack.export.exporters as _exp

        monkeypatch.setattr(_exp, "_doc_sign", lambda inv: 1)

        exporter = XeroLedgerExporter()
        rows = exporter.rows([_credit_note_inv("purchase")], "purchase")
        row = rows[0]

        assert row["*UnitAmount"] > 0, (
            "Causal guard: with _doc_sign=+1, Xero *UnitAmount is positive (flip removed)"
        )
        assert row["TaxAmount"] > 0, (
            "Causal guard: with _doc_sign=+1, Xero TaxAmount is positive (flip removed)"
        )
