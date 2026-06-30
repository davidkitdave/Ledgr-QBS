"""Tests for per-document ledger dedupe identity (WS-5.4)."""

from datetime import date

from ledgr_slack.ledger_doc_identity import (
    ledger_doc_identity,
    ledger_doc_key_for_invoice,
    ledger_row_signature,
    sheet_lacks_invoice_identity_column,
)
from ledgr_slack.export.exporters import get_exporter
from ledgr_slack.export.models import InvoiceLine, NormalizedInvoice


def test_ledger_doc_identity_includes_page_range_and_reference():
    key = ledger_doc_identity("Purchase", "INV-A", (1, 1))
    assert key == "Purchase:INV-A:1-1"


def test_ledger_doc_identity_distinct_pages_same_reference():
    k1 = ledger_doc_identity("Purchase", "INV-SAME", (1, 2))
    k2 = ledger_doc_identity("Purchase", "INV-SAME", (3, 4))
    assert k1 != k2


def test_ledger_doc_identity_stable_without_file_id():
    """Re-drop idempotency: same reference+pages → same key (no Slack file id)."""
    k_first_drop = ledger_doc_identity("Purchase", "INV-200", (1, 1))
    k_redrop = ledger_doc_identity("Purchase", "INV-200", (1, 1))
    assert k_first_drop == k_redrop == "Purchase:INV-200:1-1"


def test_ledger_doc_identity_fallback_index_when_reference_missing():
    assert ledger_doc_identity("Purchase", None, (2, 2), index=1) == "Purchase:i1:2-2"
    assert ledger_doc_identity("Purchase", "", None, index=0) == "Purchase:i0"


# --------------------------------------------------------------------------- #
# Row-signature fallback (issue #34)
# --------------------------------------------------------------------------- #


def _sales_invoice() -> NormalizedInvoice:
    inv = NormalizedInvoice(
        doc_type="sales",
        invoice_number="INV-S1",
        invoice_date=date(2025, 9, 10),
        currency="MYR",
        doc_subtotal=500.0,
    )
    inv.customer.name = "Acme Sdn Bhd"
    inv.lines = [
        InvoiceLine(description="Consulting", net_amount=500.0, tax_treatment="SR", account_code="4000")
    ]
    return inv


def test_sheet_lacks_invoice_identity_only_autocount_sales():
    """The fallback guard fires for AutoCount sales ONLY — never the others."""
    assert sheet_lacks_invoice_identity_column(get_exporter("autocount"), "sales") is True
    # AutoCount purchase has SupplierInvoiceNo; QBS/Xero/SQL all have an invoice
    # number column for both directions.
    assert sheet_lacks_invoice_identity_column(get_exporter("autocount"), "purchase") is False
    for sw in ("qbs", "xero", "sql_account"):
        assert sheet_lacks_invoice_identity_column(get_exporter(sw), "sales") is False
        assert sheet_lacks_invoice_identity_column(get_exporter(sw), "purchase") is False


def test_ledger_row_signature_normalizes_int_float_and_blank():
    """openpyxl reads 500.0 back as int 500 and a blank cell as None — both
    must hash identically to the append-side float/'' or the purge misses."""
    append_side = ledger_row_signature("Sales", "10/09/2025", "", 500.0)
    clear_side = ledger_row_signature("Sales", "10/09/2025", None, 500)
    assert append_side == clear_side == "Sales:sig:10/09/2025||500.0"


def test_ledger_row_signature_distinguishes_amount_and_code():
    base = ledger_row_signature("Sales", "10/09/2025", "ACME", 500.0)
    assert base != ledger_row_signature("Sales", "10/09/2025", "ACME", 600.0)
    assert base != ledger_row_signature("Sales", "10/09/2025", "OTHER", 500.0)
    assert base != ledger_row_signature("Sales", "11/09/2025", "ACME", 500.0)


def test_doc_key_for_invoice_autocount_sales_uses_signature():
    inv = _sales_invoice()
    key = ledger_doc_key_for_invoice(get_exporter("autocount"), "Sales", inv, 0)
    assert key.startswith("Sales:sig:")
    # DocDate + (empty) DebtorCode + Amount, NOT the invoice_number.
    assert "INV-S1" not in key


def test_doc_key_for_invoice_other_erps_keep_invoice_number():
    """QBS/Xero/SQL/AutoCount-purchase keep the invoice_number identity (no sig)."""
    inv = _sales_invoice()
    for sw in ("qbs", "xero", "sql_account"):
        key = ledger_doc_key_for_invoice(get_exporter(sw), "Sales", inv, 0)
        assert key == "Sales:INV-S1", f"{sw} should keep invoice_number identity, got {key}"
    # AutoCount purchase keeps SupplierInvoiceNo identity too.
    pinv = NormalizedInvoice(
        doc_type="purchase", invoice_number="SI-9", invoice_date=date(2025, 9, 1), doc_subtotal=10.0
    )
    pinv.supplier.name = "Vend"
    pinv.lines = [InvoiceLine(description="x", net_amount=10.0, tax_treatment="SR", account_code="6000")]
    assert ledger_doc_key_for_invoice(get_exporter("autocount"), "Purchase", pinv, 0) == "Purchase:SI-9"
