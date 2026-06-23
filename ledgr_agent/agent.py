from __future__ import annotations

from google.adk.agents import Agent

from invoice_processing.shared_libraries.model_config import lite_model
from ledgr_agent.tools import inspect_market_policy, process_document_batch


root_agent = Agent(
    name="root_accountant_agent",
    model=lite_model(),
    description="Clean Ledgr accountant agent for policy-aware document processing.",
    instruction=(
        "You are the Ledgr accountant agent. "
        "Use inspect_market_policy to explain SG/MY tax policy summaries. "
        "Use process_document_batch when the user asks to process invoice or bank files "
        "for a known client_id and explicit file paths. "
        "Gemini reads document evidence, Python checks accounting rules, and YAML stores market policy."
    ),
    tools=[inspect_market_policy, process_document_batch],
)
