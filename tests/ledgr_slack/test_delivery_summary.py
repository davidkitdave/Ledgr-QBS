"""Unit tests for Slack delivery summary metadata lines."""

from __future__ import annotations

from ledgr_slack.delivery import compose_delivery_summary


def test_compose_delivery_summary_includes_document_headers() -> None:
    workbook = {
        "documents_summary": [
            {
                "vendor_name": "Fictional Supplies Pte Ltd",
                "invoice_number": "INV-SG-001",
                "invoice_date": "2026-03-15",
                "currency": "SGD",
                "grand_total": 109.0,
                "tax_total": 9.0,
                "document_kind": "invoice",
            }
        ],
    }
    payload = {
        "kind": "invoice",
        "fy": "2026",
        "batches": [{"rows": [{"Description": "Office stationery"}]}],
    }
    summary = compose_delivery_summary(workbook, payload)
    assert "FY2026 ledger" in summary
    assert "Fictional Supplies Pte Ltd" in summary
    assert "INV-SG-001" in summary
    assert "109.00" in summary


def test_compose_delivery_summary_skips_soa_in_headers() -> None:
    workbook = {
        "documents_summary": [
            {
                "vendor_name": "Fictional Supplies Sdn Bhd",
                "invoice_number": "SOA-001",
                "document_kind": "statement_of_account",
            }
        ],
    }
    payload = {
        "kind": "invoice",
        "fy": "2026",
        "batches": [{"rows": [{}]}],
    }
    summary = compose_delivery_summary(workbook, payload)
    assert "SOA-001" not in summary


def test_compose_delivery_summary_lists_multiple_fy_destinations() -> None:
    workbook = {"documents_summary": []}
    payload = {
        "kind": "invoice",
        "fy": "2025",
        "client_name": "Acme",
        "software": "QBS Ledger",
        "batches": [
            {"fy": "2025", "rows": [{}]},
            {"fy": "2026", "rows": [{}]},
        ],
    }
    append_result = {
        "fy_groups": [
            {"fy": "2025", "n_rows": 10, "n_docs": 5},
            {"fy": "2026", "n_rows": 3, "n_docs": 2},
        ],
    }
    summary = compose_delivery_summary(workbook, payload, append_result=append_result)
    assert "FY2025" in summary
    assert "FY2026" in summary
    assert "5 document" in summary
