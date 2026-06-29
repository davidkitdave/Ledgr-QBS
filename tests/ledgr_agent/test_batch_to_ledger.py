from ledgr_agent.slack.batch_to_ledger import ledger_payload_from_batch_result


def test_ledger_payload_maps_posted_qbs_rows() -> None:
    batch = {
        "client_id": "c1",
        "status": "success",
        "per_file": [{"doc_type": "invoice", "file_name": "inv.pdf"}],
        "posted_documents": [
            {
                "doc_type": "invoice",
                "invoice_number": "INV-1234",
                "sheet": "Purchase",
                "file_name": "inv.pdf",
            }
        ],
        "export_rows": [
            {
                "workbook": "Ledger_FY2026.xlsx",
                "sheet": "Purchase",
                "Date": "2026-06-24",
                "Description": "Office supplies",
                "Account": "6100",
                "Amount": 109.0,
                "Invoice Number": "INV-1234",
            }
        ],
    }

    payload = ledger_payload_from_batch_result(
        batch,
        client_id="c1",
        client_name="Test Client",
        software="QBS Ledger",
        file_id="F1",
        source_filename="inv.pdf",
    )

    assert payload["fy"] == "2026"
    assert payload["kind"] == "invoice"
    assert payload["software"] == "QBS Ledger"
    assert payload["file_id"] == "F1"
    assert len(payload["batches"]) == 1
    assert payload["batches"][0]["sheet"] == "Purchase"
    assert payload["batches"][0]["doc_key"] == "Purchase:INV-1234"
    assert payload["batches"][0]["rows"] == [
        {
            "Date": "2026-06-24",
            "Description": "Office supplies",
            "Account": "6100",
            "Amount": 109.0,
            "Invoice Number": "INV-1234",
        }
    ]


def test_ledger_payload_filters_unposted_export_rows() -> None:
    batch = {
        "client_id": "c1",
        "status": "needs_review",
        "per_file": [{"doc_type": "invoice", "file_name": "mix.pdf"}],
        "posted_documents": [
            {"doc_type": "invoice", "invoice_number": "INV-A", "sheet": "Purchase"}
        ],
        "skipped_documents": [
            {"doc_type": "invoice", "invoice_number": "INV-B", "sheet": "Purchase"}
        ],
        "export_rows": [
            {
                "workbook": "Ledger_FY2026.xlsx",
                "sheet": "Purchase",
                "Invoice Number": "INV-A",
                "Amount": 10.0,
            },
            {
                "workbook": "Ledger_FY2026.xlsx",
                "sheet": "Purchase",
                "Invoice Number": "INV-B",
                "Amount": 20.0,
            },
        ],
    }

    payload = ledger_payload_from_batch_result(
        batch,
        client_id="c1",
        client_name="Test Client",
        software="QBS Ledger",
        file_id="F2",
    )

    assert len(payload["batches"]) == 1
    assert payload["batches"][0]["doc_key"] == "Purchase:INV-A"
    assert payload["batches"][0]["rows"][0]["Invoice Number"] == "INV-A"


def test_ledger_payload_bank_kind_uses_sheet_identity() -> None:
    batch = {
        "client_id": "c1",
        "status": "success",
        "per_file": [{"doc_type": "bank_statement", "file_name": "stmt.pdf"}],
        "posted_documents": [{"doc_type": "bank_statement", "file_name": "stmt.pdf"}],
        "export_rows": [
            {
                "workbook": "BankStatement_FY2026.xlsx",
                "sheet": "DBS 123456 SGD",
                "Date": "01/01/2026",
                "Description": "Transfer",
                "Amount": 100.0,
            }
        ],
    }

    payload = ledger_payload_from_batch_result(
        batch,
        client_id="c1",
        client_name="Test Client",
        software="QBS Ledger",
        file_id="F3",
    )

    assert payload["kind"] == "bank"
    assert payload["fy"] == "2026"
    assert payload["batches"][0]["sheet"] == "DBS 123456 SGD"
    assert payload["batches"][0]["doc_key"] == "DBS 123456 SGD:DBS 123456 SGD"


def test_ledger_payload_autocount_sales_uses_row_signature_doc_key() -> None:
    """Clean-agent mapper must match legacy append keys for AutoCount AR (#34)."""
    from datetime import date

    from accounting_agents.ledger_doc_identity import ledger_doc_key_for_invoice
    from invoice_processing.export.exporters import get_exporter
    from invoice_processing.export.models import InvoiceLine, NormalizedInvoice

    inv = NormalizedInvoice(
        doc_type="sales",
        invoice_number="INV-S1",
        invoice_date=date(2025, 9, 10),
        currency="MYR",
        doc_subtotal=500.0,
    )
    inv.customer.name = "Acme Sdn Bhd"
    inv.lines = [
        InvoiceLine(
            description="Consulting Sep",
            net_amount=500.0,
            tax_treatment="SR",
            account_code="4000",
        )
    ]
    exporter = get_exporter("autocount")
    export_row = exporter.rows([inv], "sales")[0]
    expected_key = ledger_doc_key_for_invoice(exporter, "Sales", inv, 0)

    batch = {
        "client_id": "c1",
        "status": "success",
        "per_file": [{"doc_type": "invoice", "file_name": "sales.pdf"}],
        "posted_documents": [
            {"doc_type": "invoice", "invoice_number": "INV-S1", "sheet": "Sales"}
        ],
        "export_rows": [
            {
                "workbook": "Ledger_FY2026.xlsx",
                "sheet": "Sales",
                **export_row,
            }
        ],
    }

    payload = ledger_payload_from_batch_result(
        batch,
        client_id="c1",
        client_name="Test Client",
        software="autocount",
        file_id="F4",
    )

    assert payload["batches"][0]["doc_key"] == expected_key
    assert payload["batches"][0]["doc_key"].startswith("Sales:sig:")
    assert "INV-S1" not in payload["batches"][0]["doc_key"]
