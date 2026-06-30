from ledgr_agent.agent import root_agent


def _tool_names() -> set[str]:
    names: set[str] = set()
    for tool in root_agent.tools:
        names.add(getattr(tool, "__name__", getattr(tool, "name", "")))
    return names


def test_clean_root_agent_imports() -> None:
    assert root_agent.name == "root_accountant_agent"
    names = _tool_names()
    assert names == {"read_doc", "build_sheets", "read_credit_balance"}
