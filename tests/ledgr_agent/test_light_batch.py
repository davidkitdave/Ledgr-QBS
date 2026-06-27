"""Hermetic tests for the light process_document_batch path."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from ledgr_agent.models.client_context import playground_default_context
from ledgr_agent.pipeline.light_batch import process_batch_light_async
from ledgr_agent.tools.document_tools import process_document_batch


def _bill_payload(invoice_number: str = "INV-1") -> dict:
    return {
        "file_kind": "commercial_documents",
        "documents": [
            {
                "doc_type": "purchase",
                "document_kind": "invoice",
                "vendor_name": "Acme",
                "invoice_number": invoice_number,
                "invoice_date": "2026-01-15",
                "currency": "SGD",
                "grand_total": 109.0,
                "lines": [
                    {
                        "description": "Widget",
                        "net_amount": 100.0,
                        "tax_amount": 9.0,
                        "total_amount": 109.0,
                    }
                ],
            }
        ],
        "document_count": 1,
        "extraction_meta": {
            "gemini_call_count": 1,
            "model": "gemini-2.5-flash-lite",
        },
    }


def _bank_payload() -> dict:
    return {
        "file_kind": "bank_statement",
        "accounts": [
            {
                "bank_name": "OCBC",
                "account_number": "123",
                "currency": "SGD",
                "opening_balance": 1000.0,
                "transactions": [
                    {
                        "date": "2025-01-01",
                        "description": "Deposit",
                        "deposit": 50.0,
                        "balance": 1050.0,
                    }
                ],
            }
        ],
        "extraction_meta": {
            "gemini_call_count": 1,
            "extract_mode": "digital",
        },
    }


def test_process_batch_light_parallel_fan_out(tmp_path: Path) -> None:
    files = [tmp_path / f"bill_{i}.pdf" for i in range(3)]
    for path in files:
        path.write_bytes(b"%PDF")

    call_order: list[str] = []

    def _bundle(path: Path) -> dict:
        call_order.append(path.name)
        return _bill_payload(invoice_number=path.stem)

    with patch("ledgr_agent.pipeline.light_batch.read_document_bundle", side_effect=_bundle):
        payload = asyncio.run(
            process_batch_light_async(
                files,
                playground_default_context(),
            )
        )

    assert len(call_order) == 3
    assert payload["llm_call_count"] == 3
    assert payload["documents_processed"] == 3
    assert payload["status"] == "success"
    assert payload["export_rows"]


def test_process_document_batch_soa_multi_doc(tmp_path: Path, monkeypatch) -> None:
    soa = tmp_path / "soa_pack.pdf"
    soa.write_bytes(b"%PDF")

    def _bundle(_path: Path) -> dict:
        return {
            "file_kind": "commercial_documents",
            "documents": [
                {
                    "doc_type": "purchase",
                    "document_kind": "invoice",
                    "vendor_name": "Vendor",
                    "invoice_number": "INV-A",
                    "lines": [{"description": "A", "net_amount": 10.0}],
                },
                {
                    "doc_type": "purchase",
                    "document_kind": "invoice",
                    "vendor_name": "Vendor",
                    "invoice_number": "INV-B",
                    "lines": [{"description": "B", "net_amount": 20.0}],
                },
            ],
            "document_count": 2,
            "extraction_meta": {"gemini_call_count": 1, "model": "gemini-2.5-flash-lite"},
        }

    monkeypatch.setattr(
        "ledgr_agent.tools.document_tools._credit_gate",
        lambda **_kw: {"allowed": True, "reason": "ok", "balance": 99},
    )

    out = process_document_batch(
        None,
        paths=[str(soa)],
        read_bundle_fn=_bundle,
    )

    assert out["llm_call_count"] == 1
    assert out["documents_processed"] == 2
    assert len(out["posted_documents"]) == 2


def test_process_document_batch_mixed_bill_and_bank(tmp_path: Path, monkeypatch) -> None:
    bill = tmp_path / "invoice.pdf"
    bank = tmp_path / "bank_statement.pdf"
    bill.write_bytes(b"%PDF")
    bank.write_bytes(b"%PDF")

    def _read(path: Path) -> dict:
        if "bank" in Path(path).name:
            return _bank_payload()
        return _bill_payload()

    monkeypatch.setattr(
        "ledgr_agent.tools.document_tools._credit_gate",
        lambda **_kw: {"allowed": True, "reason": "ok", "balance": 99},
    )

    out = process_document_batch(
        None,
        paths=[str(bill), str(bank)],
        read_bundle_fn=_read,
    )

    assert out["llm_call_count"] == 2
    assert out["documents_processed"] >= 2
    assert out["export_rows"]
