"""Single-lane eval routing — all invoice cases use ``ledgr_agent.agent``."""

from __future__ import annotations

DOC_AGENT_MODULE = "ledgr_agent.agent"


def agent_module_for_case(_eval_case_id: str) -> str:
    """Return the ADK agent module path for any eval case."""
    return DOC_AGENT_MODULE


def agent_directory() -> str:
    """agents-cli manifest ``agent_directory`` (repo-relative)."""
    return "ledgr_agent"
