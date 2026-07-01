"""Hermetic tests for the light bank-statement path."""

from __future__ import annotations

from types import SimpleNamespace


from ledgr_agent.internal.export import build_bank_workbook
from ledgr_agent.internal.normalize import bank_sheet_title, reconcile_running_balance
from ledgr_agent.tools.build_sheets import build_sheets
from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY


def _make_digital_pdf_bytes() -> bytes:
    body = (
        "BT /F1 12 Tf 50 750 Td "
        "(Bank Statement 2025-01-01 Account 12345678 SGD) Tj "
        "0 -20 Td (Opening Balance 1000.00) Tj "
        "0 -20 Td (01 Jan 2025 Transfer In 500.00 1500.00) Tj "
        "0 -20 Td (02 Jan 2025 GIRO Payment 200.00 1300.00) Tj "
        "0 -20 Td (03 Jan 2025 ATM Withdrawal 100.00 1200.00) Tj "
        "0 -20 Td (04 Jan 2025 Interest Credit 12.50 1212.50) Tj "
        "0 -20 Td (Closing Balance 1212.50) Tj "
        "ET"
    )
    content = body.encode()
    content_len = len(content)
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
        + f"4 0 obj\n<< /Length {content_len} >>\nstream\n".encode()
        + content
        + b"\nendstream\nendobj\n"
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
        b"xref\n0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000266 00000 n \n"
        b"0000000999 00000 n \n"
        b"trailer\n<< /Size 6 /Root 1 0 R >>\n"
        b"startxref\n1099\n%%EOF\n"
    )


_FAKE_STATEMENT = {
    "accounts": [
        {
            "bank_name": "OCBC",
            "account_number": "072-955554-5",
            "currency": "SGD",
            "opening_balance": 1000.0,
            "closing_balance": 1212.5,
            "transactions": [
                {
                    "date": "2025-01-01",
                    "description": "Transfer In",
                    "withdrawal": None,
                    "deposit": 500.0,
                    "balance": 1500.0,
                },
                {
                    "date": "2025-01-02",
                    "description": "GIRO Payment",
                    "withdrawal": 200.0,
                    "deposit": None,
                    "balance": 1300.0,
                },
            ],
        },
        {
            "bank_name": "OCBC",
            "account_number": "072-955554-5",
            "currency": "USD",
            "opening_balance": 50.0,
            "closing_balance": 75.0,
            "transactions": [
                {
                    "date": "2025-01-01",
                    "description": "FX In",
                    "withdrawal": None,
                    "deposit": 25.0,
                    "balance": 75.0,
                },
            ],
        },
    ]
}


def test_bank_sheet_title_splits_currency() -> None:
    sgd = bank_sheet_title(bank_name="OCBC", account_number="072-955554-5", currency="SGD")
    usd = bank_sheet_title(bank_name="OCBC", account_number="072-955554-5", currency="USD")
    assert sgd != usd
    assert sgd.endswith("SGD")
    assert usd.endswith("USD")


def test_reconcile_running_balance_passes_clean_chain() -> None:
    account = {
        "opening_balance": 1000.0,
        "transactions": [
            {"withdrawal": None, "deposit": 500.0, "balance": 1500.0},
            {"withdrawal": 200.0, "deposit": None, "balance": 1300.0},
        ],
    }
    ok, note = reconcile_running_balance(account)
    assert ok is True
    assert "all 2 rows reconciled" in note


def test_build_bank_workbook_two_sheets_multi_ccy() -> None:
    out = build_bank_workbook(_FAKE_STATEMENT, extract_mode="digital")
    assert out["sheet_count"] == 2
    titles = {s["title"] for s in out["sheets"]}
    assert len(titles) == 2
    assert all(s["reconciled"] for s in out["sheets"])
    assert out["sheets"][0]["columns"][0] == "Date"


def test_build_sheets_bank_from_state() -> None:
    ctx = SimpleNamespace(
        state={
            "firm_id": "T_TEST",
            READ_DOC_STATE_KEY: {
                **_FAKE_STATEMENT,
                "file_kind": "bank_statement",
                "source_path": "/tmp/bank.pdf",
                "page_count": 1,
            },
        }
    )
    out = build_sheets(ctx)
    assert out["status"] == "success"
    assert out["sheet_count"] == 2
    assert ctx.state["workbook"]["sheet_count"] == 2


def test_normalize_bank_reverse_chronological() -> None:
    """Newest-first PDF rows should reconcile after normalize flips and recomputes."""
    from ledgr_agent.internal.normalize import normalize_bank_statement

    payload = {
        "accounts": [
            {
                "bank_name": "CIMB",
                "account_number": "8001234567",
                "currency": "SGD",
                "opening_balance": 1000.0,
                "closing_balance": 1212.5,
                "transactions": [
                    {
                        "date": "2025-01-04",
                        "description": "Interest",
                        "withdrawal": None,
                        "deposit": 12.5,
                        "balance": 1212.5,
                    },
                    {
                        "date": "2025-01-03",
                        "description": "ATM",
                        "withdrawal": 100.0,
                        "deposit": None,
                        "balance": 1200.0,
                    },
                    {
                        "date": "2025-01-02",
                        "description": "GIRO",
                        "withdrawal": 200.0,
                        "deposit": None,
                        "balance": 1300.0,
                    },
                    {
                        "date": "2025-01-01",
                        "description": "Transfer In",
                        "withdrawal": None,
                        "deposit": 500.0,
                        "balance": 1500.0,
                    },
                ],
            }
        ]
    }
    normalized = normalize_bank_statement(payload, extract_mode="vision")
    assert len(normalized) == 1
    assert normalized[0]["reconciled"] is True
    txns = normalized[0]["transactions"]
    assert txns[0]["date"] in ("01/01/2025", "2025-01-01")
    assert txns[-1]["balance"] == 1212.5
