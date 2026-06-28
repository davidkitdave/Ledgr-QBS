"""Workbook → Slack ledger payload mapping."""

from ledgr_agent.runtime.delivery import workbook_to_ledger_payload


def test_workbook_to_ledger_payload_qbs_purchase() -> None:
    workbook = {
        "status": "success",
        "delivery": {
            "fy": 2026,
            "kind": "invoice",
            "doc_type": "purchase",
            "sheet": "Purchase",
            "invoice_number": "INV-1234",
            "source_filename": "inv.pdf",
        },
        "sheets": [
            {
                "title": "Purchase",
                "system": "qbs",
                "rows": [
                    {
                        "Date": "24/06/2026",
                        "Description": "Office supplies",
                        "Amount": 109.0,
                        "Invoice Number": "INV-1234",
                    }
                ],
            }
        ],
    }
    payload = workbook_to_ledger_payload(
        workbook,
        client_id="c1",
        client_name="Test Client",
        software="qbs",
        file_id="F1",
        source_filename="inv.pdf",
    )
    assert payload["fy"] == "2026"
    assert payload["kind"] == "invoice"
    assert payload["software"] == "qbs"
    assert len(payload["batches"]) == 1
    assert payload["batches"][0]["sheet"] == "Purchase"
    assert payload["batches"][0]["doc_key"] == "Purchase:INV-1234"
    assert payload["batches"][0]["rows"][0]["Invoice Number"] == "INV-1234"


def test_workbook_to_ledger_payload_bank_tabs() -> None:
    workbook = {
        "status": "success",
        "delivery": {
            "fy": 2025,
            "kind": "bank",
            "doc_type": "bank_statement",
            "sheet": "DBS",
            "source_filename": "stmt.pdf",
        },
        "sheets": [
            {
                "title": "DBS",
                "rows": [{"Date": "01/01/2025", "Description": "Paynow"}],
            }
        ],
    }
    payload = workbook_to_ledger_payload(
        workbook,
        client_id="c1",
        client_name="Test",
        software="qbs",
        file_id="F-bank",
        source_filename="stmt.pdf",
    )
    assert payload["kind"] == "bank"
    assert payload["batches"][0]["sheet"] == "DBS"
