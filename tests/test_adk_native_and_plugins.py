"""P6–P8: native ADK audit, reflect-retry plugin, chat skills."""

from __future__ import annotations

import asyncio

from accounting_agents.agent import assistant_app
from accounting_agents.chat_skills import load_chat_skills
from accounting_agents.plugins.ledgr_reflect_retry import LedgrReflectRetryPlugin


def test_tools_py_not_imported_by_production_modules():
    """ADR-0013: prototype ``tools.py`` must not be wired into live paths."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "accounting_agents"
    paths = [
        root / "agent.py",
        root / "assistant" / "__init__.py",
        root / "assistant" / "agent_def.py",
        root / "slack_runner.py",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "accounting_agents.tools" not in text
        assert "from .tools import" not in text


def test_assistant_agent_has_no_builtin_gemini_tools():
    """ADR-0013: built-ins cannot coexist with custom ledger tools."""
    from accounting_agents.assistant import ledger_analyst, ledger_corrections

    tool_names = []
    for agent in (ledger_analyst, ledger_corrections):
        for t in agent.tools:
            name = getattr(t, "name", None) or getattr(t, "__name__", str(t))
            tool_names.append(name)
    forbidden = {"google_search", "code_execution", "BuiltInCodeExecutor"}
    assert not forbidden.intersection(set(tool_names))


def test_assistant_app_wires_reflect_retry_plugin():
    assert len(assistant_app.plugins) == 1
    assert isinstance(assistant_app.plugins[0], LedgrReflectRetryPlugin)
    assert assistant_app.plugins[0].max_retries == 2


def test_ledgr_reflect_retry_treats_not_found_json_as_retryable():
    plugin = LedgrReflectRetryPlugin(max_retries=2)
    payload = '{"status": "not_found", "message": "No matching delivery."}'
    error = asyncio.run(
        plugin.extract_error_from_result(
            tool=type("T", (), {"name": "get_document_processing_detail"})(),
            tool_args={},
            tool_context=type("C", (), {})(),
            result=payload,
        )
    )
    assert error == {"status": "not_found", "message": "No matching delivery."}


def test_ledgr_reflect_retry_treats_error_dict_as_retryable():
    plugin = LedgrReflectRetryPlugin(max_retries=2)
    error = asyncio.run(
        plugin.extract_error_from_result(
            tool=type("T", (), {"name": "lookup_row"})(),
            tool_args={"query": "acme"},
            tool_context=type("C", (), {})(),
            result={"status": "error", "message": "ledger not loaded"},
        )
    )
    assert error is not None
    assert error["status"] == "error"


def test_ledgr_reflect_retry_ignores_success_payload():
    plugin = LedgrReflectRetryPlugin(max_retries=2)
    error = asyncio.run(
        plugin.extract_error_from_result(
            tool=type("T", (), {"name": "bank_totals"})(),
            tool_args={},
            tool_context=type("C", (), {})(),
            result='{"status": "success", "withdrawals": 100}',
        )
    )
    assert error is None


def test_chat_skills_load_from_skill_md():
    skills = load_chat_skills()
    assert len(skills) == 3
    names = {s.name for s in skills}
    assert names == {"ledger-read", "extraction-introspect", "write-gated"}
