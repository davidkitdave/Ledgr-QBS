"""Tests for invoice_processing.export.routing — document routing (spec §4).

All cases from §4 of the design spec:
docs/superpowers/specs/2026-06-12-ledgr-client-onboarding-fy-routing-design.md
"""

from datetime import date

from invoice_processing.export.routing import DocRoute, route_document


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _route(**kwargs) -> DocRoute:
    """Thin wrapper with project-wide test defaults (fye_month=3, client_id='cast-unity')."""
    defaults = dict(fye_month=3, client_id="cast-unity")
    defaults.update(kwargs)
    return route_document(**defaults)


# --------------------------------------------------------------------------- #
# Purchase invoice
# --------------------------------------------------------------------------- #
class TestPurchaseInvoice:
    def test_basic(self):
        r = _route(doc_type="invoice", direction="purchase",
                   doc_date=date(2025, 3, 15), filename="BV-1.pdf")
        assert r.fy == 2025
        assert r.bucket == "purchase"
        assert r.sheet == "Purchase"
        assert r.workbook == "Ledger_FY2025.xlsx"
        assert r.archive_path == "cast-unity/FY2025/purchase/BV-1.pdf"


# --------------------------------------------------------------------------- #
# Sales invoice — crosses FYE boundary
# --------------------------------------------------------------------------- #
class TestSalesInvoice:
    def test_after_fye_routes_to_next_fy(self):
        # 2025-04-02 is after the March FYE → FY2026
        r = _route(doc_type="invoice", direction="sales",
                   doc_date=date(2025, 4, 2), filename="INV-999.pdf")
        assert r.fy == 2026
        assert r.bucket == "sales"
        assert r.sheet == "Sales"
        assert r.workbook == "Ledger_FY2026.xlsx"
        assert r.archive_path == "cast-unity/FY2026/sales/INV-999.pdf"


# --------------------------------------------------------------------------- #
# Receipt — always purchase
# --------------------------------------------------------------------------- #
class TestReceipt:
    def test_receipt_direction_none(self):
        r = _route(doc_type="receipt", direction=None,
                   doc_date=date(2025, 3, 15), filename="rcpt-42.pdf")
        assert r.fy == 2025
        assert r.bucket == "purchase"
        assert r.sheet == "Purchase"
        assert r.workbook == "Ledger_FY2025.xlsx"


# --------------------------------------------------------------------------- #
# Bank statement — calendar-year FYE (fye_month=12), late-arriving Dec doc
# --------------------------------------------------------------------------- #
class TestBankStatement:
    def test_late_arriving_dec_doc_calendar_year(self):
        # TC Studio: Dec-2024 statement processed later; fye_month=12 → FY2024
        r = route_document(
            doc_type="bank_statement",
            direction=None,
            doc_date=date(2024, 12, 20),
            fye_month=12,
            client_id="tc-studio",
            filename="bank-dec-2024.pdf",
        )
        assert r.fy == 2024
        assert r.bucket == "bank"
        assert r.sheet is None
        assert r.workbook == "BankStatement_FY2024.xlsx"
        assert r.archive_path == "tc-studio/FY2024/bank/bank-dec-2024.pdf"

    def test_bank_alias(self):
        # "bank" shorthand accepted as well
        r = route_document(
            doc_type="bank",
            direction=None,
            doc_date=date(2025, 6, 1),
            fye_month=12,
            client_id="client-x",
            filename="stmt.pdf",
        )
        assert r.bucket == "bank"
        assert r.sheet is None
        assert r.workbook == "BankStatement_FY2025.xlsx"


# --------------------------------------------------------------------------- #
# Invoice with direction=None defaults to purchase
# --------------------------------------------------------------------------- #
class TestInvoiceDirectionNone:
    def test_none_direction_defaults_to_purchase(self):
        r = _route(doc_type="invoice", direction=None,
                   doc_date=date(2025, 3, 15), filename="mystery.pdf")
        assert r.bucket == "purchase"
        assert r.sheet == "Purchase"
        assert r.workbook == "Ledger_FY2025.xlsx"

    def test_unknown_direction_defaults_to_purchase(self):
        r = _route(doc_type="invoice", direction="other",
                   doc_date=date(2025, 3, 15), filename="mystery.pdf")
        assert r.bucket == "purchase"
        assert r.sheet == "Purchase"


# --------------------------------------------------------------------------- #
# Case-insensitivity
# --------------------------------------------------------------------------- #
class TestCaseInsensitivity:
    def test_mixed_case_doc_type_and_direction(self):
        r = _route(doc_type="Invoice", direction="SALES",
                   doc_date=date(2025, 4, 2), filename="INV-1.pdf")
        assert r.bucket == "sales"
        assert r.sheet == "Sales"
        assert r.workbook == "Ledger_FY2026.xlsx"

    def test_uppercase_bank_statement(self):
        r = route_document(
            doc_type="BANK_STATEMENT",
            direction=None,
            doc_date=date(2025, 6, 1),
            fye_month=12,
            client_id="c",
            filename="b.pdf",
        )
        assert r.bucket == "bank"
        assert r.sheet is None

    def test_mixed_case_receipt(self):
        r = _route(doc_type="Receipt", direction=None,
                   doc_date=date(2025, 3, 15), filename="r.pdf")
        assert r.bucket == "purchase"
        assert r.sheet == "Purchase"


# --------------------------------------------------------------------------- #
# DocRoute is frozen / immutable
# --------------------------------------------------------------------------- #
class TestDocRouteImmutable:
    def test_frozen(self):
        import pytest
        r = _route(doc_type="invoice", direction="purchase",
                   doc_date=date(2025, 3, 15), filename="f.pdf")
        with pytest.raises((AttributeError, TypeError)):
            r.fy = 9999  # type: ignore[misc]
