"""Hermetic tests for read_doc and document schemas."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import ledgr_agent.billing as billing
from ledgr_agent.agent import root_agent
from ledgr_agent.billing import CreditService, InMemoryCreditStore, configure_shared_credit_service
from ledgr_agent.internal.schemas import BUNDLE_READER_INSTRUCTION, READER_INSTRUCTION, ReadDocument, ReadDocumentBundle
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


def test_schema_exposes_doc_type_and_totals() -> None:
    doc = ReadDocument(
        doc_type="purchase",
        vendor_name="Acme Pte Ltd",
        invoice_number="INV-1",
        lines=[{"description": "Widget", "net_amount": 100.0, "total_amount": 100.0}],
    )
    assert doc.doc_type == "purchase"
    assert doc.lines[0].net_amount == 100.0


def test_bundle_instruction_describes_file_kind() -> None:
    text = BUNDLE_READER_INSTRUCTION.lower()
    assert "file_kind" in text
    assert "bank_statement" in text
    assert "commercial_documents" in text


def test_reader_instruction_describes_layout_rule() -> None:
    text = READER_INSTRUCTION.lower()
    assert "purchase" in text
    assert "bill to" in text or "billed to" in text
    assert "reconcile" in text


def test_root_agent_has_read_doc_and_build_sheets() -> None:
    tool_names = {getattr(t, "__name__", getattr(t, "name", "")) for t in root_agent.tools}
    assert tool_names == {"read_doc", "build_sheets", "read_credit_balance"}


@patch("ledgr_agent.tools.read_doc.make_client")
def test_read_doc_commercial_from_disk_path(mock_make_client, tmp_path) -> None:
    pdf = tmp_path / "bill.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    bundle = ReadDocumentBundle(
        file_kind="commercial_documents",
        documents=[
            ReadDocument(
                doc_type="purchase",
                vendor_name="Acme Pte Ltd",
                invoice_number="INV-1",
                lines=[{"description": "Widget", "net_amount": 100.0, "total_amount": 100.0}],
            )
        ],
        document_count=1,
    )
    mock_resp = MagicMock()
    mock_resp.text = bundle.model_dump_json()
    mock_resp.usage_metadata = SimpleNamespace(
        prompt_token_count=1200,
        candidates_token_count=300,
        total_token_count=1500,
    )
    mock_make_client.return_value.models.generate_content.return_value = mock_resp

    ctx = SimpleNamespace(state={"firm_id": "T_TEST"})
    out = read_doc(ctx, paths=[str(pdf)])

    assert out["status"] == "success"
    assert out["file_kind"] == "commercial_documents"
    assert out["vendor_name"] == "Acme Pte Ltd"
    assert out["extraction_meta"]["gemini_call_count"] == 1
    assert ctx.state[READ_DOC_STATE_KEY]["documents"][0]["vendor_name"] == "Acme Pte Ltd"
    mock_make_client.return_value.models.generate_content.assert_called_once()


@patch("ledgr_agent.tools.read_doc.make_client")
@patch("ledgr_agent.tools.read_doc.resolve_document_paths")
def test_read_doc_playground_upload(mock_resolve, mock_make_client, tmp_path) -> None:
    staged = tmp_path / "upload.pdf"
    staged.write_bytes(b"%PDF staged")
    mock_resolve.return_value = ([staged], [], {"source_resolution": "playground_upload"})

    bundle = ReadDocumentBundle(
        file_kind="commercial_documents",
        documents=[
            ReadDocument(
                doc_type="purchase",
                vendor_name="Vendor From Upload",
                lines=[{"description": "Line", "net_amount": 50.0}],
            )
        ],
        document_count=1,
    )
    mock_resp = MagicMock()
    mock_resp.text = bundle.model_dump_json()
    mock_make_client.return_value.models.generate_content.return_value = mock_resp

    ctx = SimpleNamespace(state={"firm_id": "T_TEST"})
    out = read_doc(ctx, paths=[])

    assert out["vendor_name"] == "Vendor From Upload"
    mock_resolve.assert_called_once_with(ctx, [])


def test_read_doc_no_file_returns_error() -> None:
    with patch(
        "ledgr_agent.tools.read_doc.resolve_document_paths",
        return_value=([], [], {}),
    ):
        out = read_doc(SimpleNamespace(state={}), paths=[])
    assert out["status"] == "error"
    assert "No document found" in out["message"]
