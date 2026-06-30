"""Backward-compat re-exports — see :mod:`accounting_agents.assistant.tools.introspect`."""

from accounting_agents.assistant.tools.introspect import (
    diagnose_assistant_context,
    explain_posted_line,
    get_document_processing_detail,
    list_pending_reviews,
    list_processing_history,
)

__all__ = [
    "diagnose_assistant_context",
    "get_document_processing_detail",
    "list_pending_reviews",
    "list_processing_history",
    "explain_posted_line",
]
