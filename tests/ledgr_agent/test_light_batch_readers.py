"""Hermetic tests for light batch readers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ledgr_agent.agents.document_reader import ReadDocument
from ledgr_agent.extract.document_bundle import read_document_bundle
from ledgr_agent.models.document_bundle import ReadDocumentBundle
from ledgr_agent.extract.bank_statement import read_bank


@patch("ledgr_agent.extract.document_bundle.make_client")
def test_read_document_bundle_single_bill(mock_make_client, tmp_path: Path) -> None:
    pdf = tmp_path / "bill.pdf"
    pdf.write_bytes(b"%PDF fake")

    bundle = ReadDocumentBundle(
        file_kind="commercial_documents",
        documents=[
            ReadDocument(
                doc_type="purchase",
                vendor_name="Acme",
                invoice_number="INV-1",
                lines=[{"description": "Widget", "net_amount": 100.0, "total_amount": 100.0}],
            )
        ],
        document_count=1,
    )
    mock_resp = MagicMock()
    mock_resp.text = bundle.model_dump_json()
    mock_resp.usage_metadata = SimpleNamespace(
        prompt_token_count=900,
        candidates_token_count=200,
        total_token_count=1100,
    )
    mock_make_client.return_value.models.generate_content.return_value = mock_resp

    out = read_document_bundle(pdf)
    assert out["file_kind"] == "commercial_documents"
    assert out["document_count"] == 1
    assert out["documents"][0]["invoice_number"] == "INV-1"
    assert out["extraction_meta"]["gemini_call_count"] == 1
    mock_make_client.return_value.models.generate_content.assert_called_once()


@patch("ledgr_agent.extract.document_bundle.make_client")
def test_read_document_bundle_soa_multi_doc(mock_make_client, tmp_path: Path) -> None:
    pdf = tmp_path / "soa_pack.pdf"
    pdf.write_bytes(b"%PDF fake")

    bundle = ReadDocumentBundle(
        file_kind="commercial_documents",
        documents=[
            ReadDocument(
                doc_type="purchase",
                vendor_name="Vendor A",
                invoice_number="INV-A",
                lines=[{"description": "Line A", "net_amount": 50.0}],
            ),
            ReadDocument(
                doc_type="purchase",
                vendor_name="Vendor A",
                invoice_number="INV-B",
                lines=[{"description": "Line B", "net_amount": 75.0}],
            ),
            ReadDocument(
                doc_type="purchase",
                vendor_name="Vendor A",
                invoice_number="INV-C",
                lines=[{"description": "Line C", "net_amount": 25.0}],
            ),
        ],
        document_count=3,
    )
    mock_resp = MagicMock()
    mock_resp.text = bundle.model_dump_json()
    mock_make_client.return_value.models.generate_content.return_value = mock_resp

    out = read_document_bundle(pdf)
    assert out["file_kind"] == "commercial_documents"
    assert out["document_count"] == 3
    assert len(out["documents"]) == 3
    assert out["extraction_meta"]["gemini_call_count"] == 1


@patch("ledgr_agent.extract.document_bundle.make_client")
def test_read_document_bundle_bank_statement(mock_make_client, tmp_path: Path) -> None:
    pdf = tmp_path / "11'25 (PBB - Sdn Bhd).pdf"
    pdf.write_bytes(b"%PDF fake")

    from ledgr_agent.models.bank_statement import BankAccount, BankTxn

    bundle = ReadDocumentBundle(
        file_kind="bank_statement",
        accounts=[
            BankAccount(
                bank_name="PBB",
                account_number="1234567",
                currency="MYR",
                opening_balance=1000.0,
                transactions=[
                    BankTxn(
                        date="2025-11-01",
                        description="Payment",
                        withdrawal=50.0,
                        balance=950.0,
                    )
                ],
            )
        ],
        document_count=0,
    )
    mock_resp = MagicMock()
    mock_resp.text = bundle.model_dump_json()
    mock_resp.usage_metadata = SimpleNamespace(
        prompt_token_count=900,
        candidates_token_count=200,
        total_token_count=1100,
    )
    mock_make_client.return_value.models.generate_content.return_value = mock_resp

    out = read_document_bundle(pdf)
    assert out["file_kind"] == "bank_statement"
    assert out["accounts"]
    assert out["accounts_normalized"]
    assert out["documents"] == []
    assert out["extraction_meta"]["gemini_call_count"] == 1


@patch("ledgr_agent.extract.bank_statement.extract_bank_statement")
def test_read_bank_returns_accounts(mock_extract, tmp_path: Path) -> None:
    pdf = tmp_path / "bank.pdf"
    pdf.write_bytes(b"%PDF fake")

    from ledgr_agent.models.bank_statement import BankAccount, BankTxn, ReadBankStatement

    mock_extract.return_value = (
        ReadBankStatement(
            accounts=[
                BankAccount(
                    bank_name="OCBC",
                    account_number="123",
                    currency="SGD",
                    opening_balance=100.0,
                    transactions=[
                        BankTxn(
                            date="2025-01-01",
                            description="Deposit",
                            deposit=50.0,
                            balance=150.0,
                        )
                    ],
                )
            ]
        ),
        "digital",
    )

    out = read_bank(pdf)
    assert out["accounts"]
    assert out["accounts_normalized"]
    assert out["extraction_meta"]["gemini_call_count"] == 1
    mock_extract.assert_called_once()
