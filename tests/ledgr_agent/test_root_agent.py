from ledgr_agent.agent import root_agent
from google.adk.tools.agent_tool import AgentTool


def _tool_names() -> set[str]:
    names: set[str] = set()
    for tool in root_agent.tools:
        if isinstance(tool, AgentTool):
            names.add(tool.agent.name)
        else:
            names.add(getattr(tool, "__name__", getattr(tool, "name", "")))
    return names


def test_clean_root_agent_imports() -> None:
    assert root_agent.name == "root_accountant_agent"
    names = _tool_names()
    assert "inspect_market_policy" in names
    assert "process_document_batch" in names
    assert "read_document" in names
    assert "project_to_erp" in names
    assert "read_bank_statement" in names
    assert "project_bank_workbook" in names
    assert "bill_pipeline" in names
    assert "bank_pipeline" in names
