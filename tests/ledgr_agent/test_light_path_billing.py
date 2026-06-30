"""Light-path billing: gate before read, charge on build_sheets."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import ledgr_agent.billing as billing
from ledgr_agent.billing import (
    CreditService,
    InMemoryCreditStore,
    billable_units,
    configure_shared_credit_service,
)
from ledgr_agent.tools.build_sheets import build_sheets
from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY, read_doc


@pytest.fixture(autouse=True)
def _credit_setup() -> None:
    billing._shared_credit_service = None
    service = CreditService(InMemoryCreditStore())
    service.ensure_firm("T_TEST")
    service.grant("T_TEST", 10, note="test")
    configure_shared_credit_service(service)
    yield
    billing._shared_credit_service = None


def test_read_doc_blocked_at_zero_balance(monkeypatch, tmp_path) -> None:
    billing.configure_shared_credit_service(CreditService(InMemoryCreditStore()))
    pdf = tmp_path / "bill.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    ctx = SimpleNamespace(state={"firm_id": "T_ZERO", "client_id": "c1"})

    def _boom(*_a, **_k):
        raise AssertionError("LLM should not be called when blocked")

    monkeypatch.setattr("ledgr_agent.tools.read_doc.make_client", _boom)

    out = read_doc(ctx, paths=[str(pdf)])
    assert out["status"] == "blocked"
    assert out["credits"]["credit_status"] == "blocked"


def test_build_sheets_charges_one_credit_for_bill() -> None:
    ctx = SimpleNamespace(
        state={
            "firm_id": "T_TEST",
            "client_id": "c1",
            READ_DOC_STATE_KEY: {
                "file_kind": "commercial_documents",
                "source_path": "/tmp/invoice.pdf",
                "documents": [
                    {
                        "vendor_name": "Acme",
                        "invoice_number": "INV-1",
                        "invoice_date": "2026-01-01",
                        "currency": "SGD",
                        "lines": [{"description": "Widget", "net_amount": 100.0}],
                    }
                ],
            },
        }
    )
    out = build_sheets(ctx)
    assert out["status"] == "success"
    assert out["credits"]["credit_status"] == "charged"
    assert out["credits"]["credits_used"] == 1
    assert billing.get_shared_credit_service().read_balance("T_TEST") == 9


def test_build_sheets_idempotent_on_same_file() -> None:
    ctx = SimpleNamespace(
        state={
            "firm_id": "T_TEST",
            "client_id": "c1",
            READ_DOC_STATE_KEY: {
                "file_kind": "commercial_documents",
                "source_path": "/tmp/same.pdf",
                "documents": [
                    {
                        "vendor_name": "Acme",
                        "invoice_number": "INV-2",
                        "lines": [{"description": "Widget", "net_amount": 50.0}],
                    }
                ],
            },
        }
    )
    build_sheets(ctx)
    build_sheets(ctx)
    assert billing.get_shared_credit_service().read_balance("T_TEST") == 9


def test_read_doc_blocked_bank_at_zero_balance(monkeypatch, tmp_path) -> None:
    billing.configure_shared_credit_service(CreditService(InMemoryCreditStore()))
    pdf = tmp_path / "bank.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    ctx = SimpleNamespace(state={"firm_id": "T_ZERO", "client_id": "c1"})

    def _boom(*_a, **_k):
        raise AssertionError("LLM should not be called when blocked")

    monkeypatch.setattr("ledgr_agent.tools.read_doc.make_client", _boom)

    out = read_doc(ctx, paths=[str(pdf)])
    assert out["status"] == "blocked"


def test_build_sheets_charges_by_page_count_for_bank() -> None:
    ctx = SimpleNamespace(
        state={
            "firm_id": "T_TEST",
            "client_id": "c1",
            READ_DOC_STATE_KEY: {
                "file_kind": "bank_statement",
                "source_path": "/tmp/bank.pdf",
                "page_count": 3,
                "accounts": [
                    {
                        "bank_name": "OCBC",
                        "account_number": "123",
                        "currency": "SGD",
                        "opening_balance": 100.0,
                        "closing_balance": 90.0,
                        "transactions": [
                            {
                                "date": "2026-01-01",
                                "description": "Pay",
                                "withdrawal": 10.0,
                                "deposit": None,
                                "balance": 90.0,
                            }
                        ],
                    }
                ],
            },
        }
    )
    out = build_sheets(ctx)
    assert out["status"] == "success"
    assert out["credits"]["credits_used"] == 3
    assert out["credits"]["credit_status"] == "charged"
    assert billing.get_shared_credit_service().read_balance("T_TEST") == 7


def test_billable_units_multi_receipt_one_page() -> None:
    assert billable_units(file_kind="commercial_documents", page_count=1, document_count=3) == 3


def test_billable_units_multi_page_single_invoice() -> None:
    assert billable_units(file_kind="commercial_documents", page_count=3, document_count=1) == 3


def _commercial_doc(invoice_number: str) -> dict:
    return {
        "vendor_name": "Acme",
        "invoice_number": invoice_number,
        "invoice_date": "2026-01-01",
        "currency": "SGD",
        "lines": [{"description": "Widget", "net_amount": 100.0}],
    }


def test_build_sheets_charges_three_for_multi_receipt() -> None:
    ctx = SimpleNamespace(
        state={
            "firm_id": "T_TEST",
            "client_id": "c1",
            READ_DOC_STATE_KEY: {
                "file_kind": "commercial_documents",
                "source_path": "/tmp/multi.pdf",
                "page_count": 1,
                "document_count": 3,
                "credit_units": 3,
                "documents": [
                    _commercial_doc("INV-1"),
                    _commercial_doc("INV-2"),
                    _commercial_doc("INV-3"),
                ],
            },
        }
    )
    out = build_sheets(ctx)
    assert out["status"] == "success"
    assert out["credits"]["credits_used"] == 3
    assert billing.get_shared_credit_service().read_balance("T_TEST") == 7


def test_build_sheets_projects_all_documents() -> None:
    ctx = SimpleNamespace(
        state={
            "firm_id": "T_TEST",
            "client_id": "c1",
            READ_DOC_STATE_KEY: {
                "file_kind": "commercial_documents",
                "source_path": "/tmp/multi.pdf",
                "page_count": 1,
                "document_count": 3,
                "credit_units": 3,
                "documents": [
                    _commercial_doc("INV-1"),
                    _commercial_doc("INV-2"),
                    _commercial_doc("INV-3"),
                ],
            },
        }
    )
    out = build_sheets(ctx)
    assert out["status"] == "success"
    # Four ERP systems × three documents = twelve sheets
    assert out["sheet_count"] == 12


def test_read_doc_regates_after_multi_receipt_read(monkeypatch, tmp_path) -> None:
    service = CreditService(InMemoryCreditStore())
    service.ensure_firm("T_TWO")
    service.grant("T_TWO", 2, note="test")
    configure_shared_credit_service(service)

    pdf = tmp_path / "multi.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    ctx = SimpleNamespace(state={"firm_id": "T_TWO", "client_id": "c1"})

    def _three_docs(_data, _mime):
        return {
            "file_kind": "commercial_documents",
            "documents": [
                _commercial_doc("INV-1"),
                _commercial_doc("INV-2"),
                _commercial_doc("INV-3"),
            ],
            "document_count": 3,
            "extraction_meta": {},
        }

    monkeypatch.setattr("ledgr_agent.tools.read_doc._read_bytes_with_gemini", _three_docs)
    monkeypatch.setattr("ledgr_agent.tools.read_doc.count_input_pages", lambda _d, _m: 1)

    out = read_doc(ctx, paths=[str(pdf)])
    assert out["status"] == "blocked"
    assert out["credits"]["credit_status"] == "blocked"
