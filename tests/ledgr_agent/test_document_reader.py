"""Hermetic tests for the document reader schema and read_document tool."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ledgr_agent.agent import root_agent
from ledgr_agent.agents.document_reader import READER_INSTRUCTION, ReadDocument
from ledgr_agent.tools.read_document_tool import read_document


def test_schema_exposes_doc_type_and_totals() -> None:
    doc = ReadDocument(
        doc_type="purchase",
        vendor_name="Acme Pte Ltd",
        invoice_number="INV-1",
        lines=[{"description": "Widget", "net_amount": 100.0, "total_amount": 100.0}],
    )
    assert doc.doc_type == "purchase"
    assert doc.lines[0].net_amount == 100.0


def test_reader_instruction_describes_layout_rule() -> None:
    text = READER_INSTRUCTION.lower()
    assert "purchase" in text
    assert "bill to" in text or "billed to" in text
    assert "reconcile" in text


def test_root_agent_has_read_document_and_project_to_erp() -> None:
    from google.adk.tools.agent_tool import AgentTool

    tool_names = set()
    agent_tool_names = set()
    for tool in root_agent.tools:
        if isinstance(tool, AgentTool):
            agent_tool_names.add(tool.agent.name)
        else:
            tool_names.add(getattr(tool, "__name__", getattr(tool, "name", "")))
    assert "read_document" in tool_names
    assert "project_to_erp" in tool_names
    assert "bill_pipeline" in agent_tool_names
    assert "bank_pipeline" in agent_tool_names


@patch("ledgr_agent.tools.read_document_tool.make_client")
def test_read_document_from_disk_path(mock_make_client, tmp_path) -> None:
    pdf = tmp_path / "bill.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    expected = ReadDocument(
        doc_type="purchase",
        vendor_name="Acme Pte Ltd",
        invoice_number="INV-1",
        lines=[{"description": "Widget", "net_amount": 100.0, "total_amount": 100.0}],
    )
    mock_resp = MagicMock()
    mock_resp.text = expected.model_dump_json()
    mock_resp.usage_metadata = SimpleNamespace(
        prompt_token_count=1200,
        candidates_token_count=300,
        total_token_count=1500,
    )
    mock_make_client.return_value.models.generate_content.return_value = mock_resp

    ctx = SimpleNamespace(state={})
    out = read_document(ctx, paths=[str(pdf)])

    assert out["vendor_name"] == "Acme Pte Ltd"
    assert out["invoice_number"] == "INV-1"
    assert out["extraction_meta"]["gemini_call_count"] == 1
    assert out["extraction_meta"]["usage"]["total_token_count"] == 1500
    assert ctx.state["read_document"]["vendor_name"] == "Acme Pte Ltd"
    mock_make_client.return_value.models.generate_content.assert_called_once()


@patch("ledgr_agent.tools.read_document_tool.make_client")
@patch("ledgr_agent.tools.read_document_tool.resolve_document_paths")
def test_read_document_playground_upload(mock_resolve, mock_make_client, tmp_path) -> None:
    staged = tmp_path / "upload.pdf"
    staged.write_bytes(b"%PDF staged")
    mock_resolve.return_value = ([staged], [], {"source_resolution": "playground_upload"})

    expected = ReadDocument(
        doc_type="purchase",
        vendor_name="Vendor From Upload",
        lines=[{"description": "Line", "net_amount": 50.0}],
    )
    mock_resp = MagicMock()
    mock_resp.text = expected.model_dump_json()
    mock_make_client.return_value.models.generate_content.return_value = mock_resp

    ctx = SimpleNamespace(state={})
    out = read_document(ctx, paths=[])

    assert out["vendor_name"] == "Vendor From Upload"
    mock_resolve.assert_called_once_with(ctx, [])


def test_read_document_no_file_returns_error() -> None:
    with patch(
        "ledgr_agent.tools.read_document_tool.resolve_document_paths",
        return_value=([], [], {}),
    ):
        out = read_document(SimpleNamespace(state={}), paths=[])
    assert out["status"] == "error"
    assert "No document found" in out["message"]
