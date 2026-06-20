"""Thin LlmAgent definitions — root coordinator + read/write specialists."""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from accounting_agents import config

from .instruction import (
    _BASE_INSTRUCTION,
    _CORRECTIONS_INSTRUCTION,
    _ROOT_INSTRUCTION,
    _assistant_before_agent,
    _chat_before_model,
)
from .tools.explain_tools import (
    explain_categorization,
    explain_document_processing,
    explain_tax_treatment,
)
from .tools.introspect import (
    diagnose_assistant_context,
    explain_posted_line,
    get_document_processing_detail,
    list_pending_reviews,
    list_processing_history,
)
from .tools.mutate_tools import (
    amend_ledger_row,
    learn_mapping,
    re_extract_document,
    remove_ledger_row,
    replace_recorded_month,
)
from .tools.read_tools import (
    bank_totals,
    gst_threshold_check,
    list_recent_documents,
    lookup_coa_account,
    lookup_row,
    model_info,
    pnl_for_fy,
    show_client_profile,
    show_learned_mappings,
    summarize_by_category,
    summarize_recent_activity,
)

_READ_TOOLS = [
    bank_totals,
    summarize_by_category,
    pnl_for_fy,
    gst_threshold_check,
    show_client_profile,
    show_learned_mappings,
    model_info,
    explain_categorization,
    lookup_coa_account,
    explain_tax_treatment,
    summarize_recent_activity,
    lookup_row,
    list_recent_documents,
    list_processing_history,
    explain_document_processing,
    get_document_processing_detail,
    explain_posted_line,
    diagnose_assistant_context,
    list_pending_reviews,
]

_MUTATE_TOOLS = [
    lookup_row,
    FunctionTool(amend_ledger_row, require_confirmation=True),
    FunctionTool(remove_ledger_row, require_confirmation=True),
    FunctionTool(replace_recorded_month, require_confirmation=True),
    FunctionTool(re_extract_document, require_confirmation=True),
    learn_mapping,
]

ledger_analyst = LlmAgent(
    name="ledger_analyst",
    model=config.MODEL_CHAT,
    description=(
        "Read-only ledger analyst: queries, summaries, COA lookups, "
        "categorization/tax explanations, and processing diagnostics."
    ),
    instruction=_BASE_INSTRUCTION,
    before_agent_callback=_assistant_before_agent,
    before_model_callback=_chat_before_model,
    tools=_READ_TOOLS,
)

ledger_corrections = LlmAgent(
    name="ledger_corrections",
    model=config.MODEL_CHAT,
    description=(
        "Ledger corrections specialist: amend/remove rows, clear a month, "
        "re-extract documents, and learn vendor mappings (gated writes)."
    ),
    instruction=_CORRECTIONS_INSTRUCTION,
    before_agent_callback=_assistant_before_agent,
    before_model_callback=_chat_before_model,
    tools=_MUTATE_TOOLS,
)

assistant_agent = LlmAgent(
    name="assistant",
    model=config.MODEL_CHAT,
    description=(
        "Accounting chat coordinator — routes to ledger_analyst (read) or "
        "ledger_corrections (mutate)."
    ),
    instruction=_ROOT_INSTRUCTION,
    before_agent_callback=_assistant_before_agent,
    before_model_callback=_chat_before_model,
    sub_agents=[ledger_analyst, ledger_corrections],
)

__all__ = [
    "assistant_agent",
    "ledger_analyst",
    "ledger_corrections",
]
