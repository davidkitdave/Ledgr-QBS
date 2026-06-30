"""Workbook → Slack ledger payload mapping (frontend package)."""

from ledgr_slack.delivery import ledger_replace_for_batches, workbook_to_ledger_payload


class _PointerStore:
    def __init__(self, seen_doc_keys: list[str] | None = None) -> None:
        self._seen = seen_doc_keys or []

    def get_pointer(self, client_id: str, fy: str) -> dict:
        return {"seen_doc_keys": list(self._seen)}


class _MultiFyPointerStore:
    def __init__(self, seen_by_fy: dict[str, list[str]]) -> None:
        self._seen_by_fy = seen_by_fy

    def get_pointer(self, client_id: str, fy: str) -> dict:
        return {"seen_doc_keys": list(self._seen_by_fy.get(fy, []))}


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

    assert len(payload["batches"]) == 3
    seen_invoices = {batch["rows"][0]["Invoice Number"] for batch in payload["batches"]}
    assert seen_invoices == set(invoice_numbers)
    doc_keys = [batch["doc_key"] for batch in payload["batches"]]
    assert len(set(doc_keys)) == 3


def test_ledger_replace_for_batches_when_doc_key_seen() -> None:
    store = _PointerStore(["Purchase:INV-1234"])
    batches = [{"doc_key": "Purchase:INV-1234", "rows": [{"Invoice Number": "INV-1234"}]}]
    assert ledger_replace_for_batches(store, client_id="c1", fy="2026", batches=batches) is True


def test_ledger_replace_for_batches_false_when_empty_rows() -> None:
    store = _PointerStore(["Purchase:INV-1234"])
    batches = [{"doc_key": "Purchase:INV-1234", "rows": []}]
    assert ledger_replace_for_batches(store, client_id="c1", fy="2026", batches=batches) is False


def test_ledger_replace_for_batches_false_for_new_doc_key() -> None:
    store = _PointerStore(["Purchase:INV-OLD"])
    batches = [{"doc_key": "Purchase:INV-NEW", "rows": [{"Invoice Number": "INV-NEW"}]}]
    assert ledger_replace_for_batches(store, client_id="c1", fy="2026", batches=batches) is False


def test_ledger_replace_for_batches_checks_each_batch_fy_not_payload_fy() -> None:
    """Re-upload across FYs must replace when a later-FY doc was already seen."""
    store = _MultiFyPointerStore(
        {
            "FY2025": ["Purchase:INV-OLD"],
            "FY2026": [],
        }
    )
    batches = [
        {
            "doc_key": "Purchase:INV-NEW",
            "fy": "FY2026",
            "rows": [{"Invoice Number": "INV-NEW"}],
        },
        {
            "doc_key": "Purchase:INV-OLD",
            "fy": "FY2025",
            "rows": [{"Invoice Number": "INV-OLD"}],
        },
    ]
    # payload.fy would be FY2026 (first doc); old code only checked FY2026 and missed INV-OLD.
    assert ledger_replace_for_batches(store, client_id="c1", fy="FY2026", batches=batches) is True
