from __future__ import annotations

from google.adk.agents import Agent

from invoice_processing.shared_libraries.model_config import lite_model
from ledgr_agent.tools import (
    amend_ledger_row_action,
    explain_tax_treatment_action,
    inspect_market_policy,
    process_document_batch,
)


root_agent = Agent(
    name="root_accountant_agent",
    model=lite_model(),
    description="Clean Ledgr accountant agent for policy-aware document processing.",
    instruction=(
        "You are the Ledgr accountant agent. "
        "Use tools to inspect market policy and explain what capabilities are available. "
        "Use process_document_batch to process batches of documents when requested by the user. "
        "Use explain_tax_treatment_action to explain the tax treatment the reasoner would assign a line. "
        "amend_ledger_row_action requires human confirmation before mutating the ledger. "
        "Gemini reads document evidence, Python checks accounting rules, and YAML stores market policy."
    ),
    tools=[
        inspect_market_policy,
        process_document_batch,
        explain_tax_treatment_action,
        amend_ledger_row_action,
    ],
)
