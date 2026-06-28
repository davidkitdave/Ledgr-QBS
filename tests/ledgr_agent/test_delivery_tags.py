"""Delivery tag helpers for build_sheets."""

from __future__ import annotations

from datetime import date

from ledgr_agent.internal.delivery_tags import build_delivery_tags, parse_document_date
from ledgr_agent.internal.fy import fy_for_date


def test_fy_for_date_calendar_year() -> None:
    assert fy_for_date(date(2025, 6, 1), 12) == 2025
    assert fy_for_date(date(2026, 1, 1), 12) == 2026


def test_fy_for_date_march_fye() -> None:
    assert fy_for_date(date(2025, 3, 15), 3) == 2025
    assert fy_for_date(date(2025, 4, 1), 3) == 2026


def test_parse_document_date_iso() -> None:
    assert parse_document_date("2026-01-15") == date(2026, 1, 15)


def test_build_delivery_tags_purchase_invoice() -> None:
    read_payload = {
        "file_kind": "commercial_documents",
        "documents": [
            {
                "doc_type": "purchase",
                "invoice_number": "INV-100",
                "invoice_date": "2026-01-15",
            }
        ],
    }
    sheets = [{"title": "Purchase", "system": "qbs", "rows": [{"Invoice Number": "INV-100"}]}]
    tags = build_delivery_tags(
        read_payload=read_payload,
        sheets=sheets,
        state={"fye_month": 12, "source_filename": "bill.pdf"},
        source_path="/tmp/bill.pdf",
        file_kind="commercial_documents",
    )
    assert tags["kind"] == "invoice"
    assert tags["doc_type"] == "purchase"
    assert tags["sheet"] == "Purchase"
    assert tags["fy"] == 2026
    assert tags["invoice_number"] == "INV-100"


def test_build_delivery_tags_bank() -> None:
    read_payload = {
        "file_kind": "bank_statement",
        "accounts": [
            {
                "transactions": [{"date": "2025-11-02", "description": "ATM"}],
            }
        ],
    }
    sheets = [{"title": "DBS-1234", "rows": [{"Date": "02/11/2025"}]}]
    tags = build_delivery_tags(
        read_payload=read_payload,
        sheets=sheets,
        state={"fye_month": 3, "source_filename": "bank.pdf"},
        source_path="/tmp/bank.pdf",
        file_kind="bank_statement",
    )
    assert tags["kind"] == "bank"
    assert tags["sheet"] == "DBS-1234"
    assert tags["fy"] == 2026
