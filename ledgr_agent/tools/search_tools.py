"""Optional ADK native web-search sub-agent (Plan 6 / playground QA)."""

from __future__ import annotations

import os

from invoice_processing.shared_libraries.model_config import lite_model

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def web_search_enabled() -> bool:
    raw = os.environ.get("LEDGR_ENABLE_WEB_SEARCH", "")
    return raw.strip().lower() in _TRUTHY


def build_web_search_agent_tool():
    """Return an ``AgentTool`` wrapping a search-only sub-agent, or ``None``."""

    if not web_search_enabled():
        return None

    from google.adk.agents import Agent
    from google.adk.tools import google_search
    from google.adk.tools.agent_tool import AgentTool

    search_agent = Agent(
        name="tax_research_agent",
        model=lite_model(),
        description="Web search for current tax and accounting guidance.",
        instruction=(
            "You answer tax and accounting questions using Google Search. "
            "Prefer official sources (IRAS, LHDN, ATO). "
            "Always cite the source URL in your reply. "
            "Do not process invoices or mutate ledgers."
        ),
        tools=[google_search],
    )
    return AgentTool(agent=search_agent)
