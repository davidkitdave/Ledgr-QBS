"""Eval routing — single-lane ``ledgr_agent`` only."""

from __future__ import annotations

DOC_AGENT_MODULE = "ledgr_agent.agent"


def agent_module_for_case(_eval_case_id: str) -> str:
    """Return the ADK agent module path for any eval case."""
    return DOC_AGENT_MODULE


def agent_directory() -> str:
    """agents-cli manifest ``agent_directory`` (repo-relative)."""
    return "ledgr_agent"


def test_doc_agent_module() -> None:
    assert agent_module_for_case("sg_gst_invoice_single") == DOC_AGENT_MODULE


def test_agent_directory_points_at_ledgr_agent() -> None:
    assert agent_directory() == "ledgr_agent"
