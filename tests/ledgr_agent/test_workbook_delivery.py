"""Workbook → Slack ledger payload mapping."""

from ledgr_slack.delivery import workbook_to_ledger_payload


def _commercial_sheet(invoice_number: str, index: int) -> dict:
    return {
        "title": "Purchase",
        "columns": ["Date", "Invoice Number", "Amount"],
        "rows": [
            {
                "Date": "28/06/2026",
                "Invoice Number": invoice_number,
                "Amount": float(100 * (index + 1)),
            }
        ],
        "system": "qbs",
        "software_name": "QBS Ledger",
        "invoice_number": invoice_number,
    }


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


def test_multi_document_commercial_produces_one_batch_per_document() -> None:
    """A 3-document workbook must yield 3 batches with distinct doc_keys."""
    invoice_numbers = ["INV-001", "INV-002", "INV-003"]
    workbook = {
        "status": "success",
        "delivery": {
            "fy": 2026,
            "kind": "invoice",
            "doc_type": "purchase",
        },
        "sheets": [_commercial_sheet(inv, i) for i, inv in enumerate(invoice_numbers)],
    }
    payload = workbook_to_ledger_payload(
        workbook,
        client_id="c-multi",
        client_name="Multi Client",
        software="qbs",
        file_id="F-multi",
        source_filename="multi.pdf",
    )

    assert len(payload["batches"]) == 3, (
        f"Expected 3 batches for 3 documents, got {len(payload['batches'])}"
    )
    # All invoice numbers appear in the batches
    seen_invoices = {batch["rows"][0]["Invoice Number"] for batch in payload["batches"]}
    assert seen_invoices == set(invoice_numbers)
    # All doc_keys are distinct
    doc_keys = [batch["doc_key"] for batch in payload["batches"]]
    assert len(set(doc_keys)) == 3, f"doc_keys are not distinct: {doc_keys}"
    # Each batch has rows
    for batch in payload["batches"]:
        assert len(batch["rows"]) == 1
