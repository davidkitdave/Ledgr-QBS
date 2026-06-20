"""Accounting chat assistant package (WS6 split).

The Slack chat lane runs ``assistant_agent`` from :mod:`accounting_agents.assistant.agent_def`
as a standalone root ``LlmAgent`` with two scoped sub-agents:

- ``ledger_analyst`` — 19 read-only query/explain tools
- ``ledger_corrections`` — 5 mutating tools + ``lookup_row`` helper

See ``docs/adr/0008-chat-lane-standalone-root-agent.md``.
"""

from __future__ import annotations

from .agent_def import assistant_agent, ledger_analyst, ledger_corrections
from .constants import (
    DOCUMENT_SESSIONS_KEY,
    GST_THRESHOLD_SGD,
    LEDGER_DATA_KEY,
    PENDING_LEARN_KEY,
    PENDING_REEXTRACT_KEY,
    PENDING_WRITE_KEY,
    PENDING_REVIEWS_KEY,
    PROCESSING_LOG_KEY,
    SST_THRESHOLD_MYR,
    THREAD_FOCUS_KEY,
)
from .instruction import (
    _BASE_INSTRUCTION,
    _chat_before_model,
    assistant_instruction,
)
from .tools._helpers import (
    _get_rows,
    _normalize_row_for_tools,
    _row_signature,
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

__all__ = [
    "assistant_agent",
    "ledger_analyst",
    "ledger_corrections",
    "assistant_instruction",
    "LEDGER_DATA_KEY",
    "PENDING_WRITE_KEY",
    "PENDING_LEARN_KEY",
    "PENDING_REEXTRACT_KEY",
    "PROCESSING_LOG_KEY",
    "DOCUMENT_SESSIONS_KEY",
    "PENDING_REVIEWS_KEY",
    "THREAD_FOCUS_KEY",
    "GST_THRESHOLD_SGD",
    "SST_THRESHOLD_MYR",
    "amend_ledger_row",
    "remove_ledger_row",
    "replace_recorded_month",
    "re_extract_document",
    "learn_mapping",
    "bank_totals",
    "summarize_by_category",
    "pnl_for_fy",
    "gst_threshold_check",
    "show_client_profile",
    "show_learned_mappings",
    "model_info",
    "explain_categorization",
    "lookup_coa_account",
    "explain_tax_treatment",
    "summarize_recent_activity",
    "lookup_row",
    "list_recent_documents",
    "list_processing_history",
    "explain_document_processing",
    "get_document_processing_detail",
    "explain_posted_line",
    "diagnose_assistant_context",
    "list_pending_reviews",
    # Backward-compat for tests / slack_runner local imports
    "_BASE_INSTRUCTION",
    "_chat_before_model",
    "_get_rows",
    "_normalize_row_for_tools",
    "_row_signature",
]
