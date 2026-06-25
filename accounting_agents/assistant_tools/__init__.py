"""Backward-compat package — tools live under :mod:`accounting_agents.assistant.tools`."""

from accounting_agents.assistant.tools.introspect import (
    diagnose_assistant_context,
    get_document_processing_detail,
    list_pending_reviews,
    list_processing_history,
)

__all__ = [
    "diagnose_assistant_context",
    "get_document_processing_detail",
    "list_pending_reviews",
    "list_processing_history",
]
