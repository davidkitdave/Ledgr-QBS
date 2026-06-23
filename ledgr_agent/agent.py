from __future__ import annotations

from google.adk.agents import Agent

from invoice_processing.shared_libraries.model_config import lite_model
from ledgr_agent.tools import inspect_market_policy


root_agent = Agent(
    name="root_accountant_agent",
    model=lite_model(),
    description="Clean Ledgr accountant agent for policy-aware document processing.",
    instruction=(
        "You are the Ledgr accountant agent. "
        "Use tools to inspect market policy and explain what capabilities are available. "
        "Do not process real private documents unless a specific document tool is available. "
        "Gemini reads document evidence, Python checks accounting rules, and YAML stores market policy."
    ),
    tools=[inspect_market_policy],
)
