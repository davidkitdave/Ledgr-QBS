from ledgr_agent.agent import root_agent
 
 
def test_clean_root_agent_imports() -> None:
    assert root_agent.name == "root_accountant_agent"
    tool_names = {getattr(tool, "__name__", getattr(tool, "name", "")) for tool in root_agent.tools}
    assert "inspect_market_policy" in tool_names
    assert "process_document_batch" in tool_names
