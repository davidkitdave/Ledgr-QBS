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


def test_document_date_from_receipt_number_yyyymmdd() -> None:
    from ledgr_agent.internal.delivery_tags import document_date_from_fields, document_sheet_meta

    doc = {
        "doc_type": "purchase",
        "invoice_number": "202602020015",
        "invoice_date": "Feb 02 22:22:28 GMT+08:00 2020",
    }
    assert document_date_from_fields(doc) == date(2026, 2, 2)
    meta = document_sheet_meta(doc, fye_month=12)
    assert meta["fy"] == 2026


def test_sanitize_outlier_document_dates_snaps_2006() -> None:
    from ledgr_agent.internal.delivery_tags import document_sheet_meta, sanitize_outlier_document_dates

    docs = [
        {"doc_type": "purchase", "invoice_date": "2025-11-15", "invoice_number": "A1"},
        {"doc_type": "purchase", "invoice_date": "2025-12-01", "invoice_number": "A2"},
        {"doc_type": "purchase", "invoice_date": "2025-12-02", "invoice_number": "A3"},
        {
            "doc_type": "purchase",
            "invoice_date": "2006-02-23",
            "invoice_number": "POS065203",
        },
    ]
    sanitize_outlier_document_dates(docs)
    meta = document_sheet_meta(docs[-1], fye_month=12)
    assert meta["fy"] == 2025


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
