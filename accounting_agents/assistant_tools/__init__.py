"""``assistant_tools`` — modular tool implementations for the chat agent.

The tools live alongside the chat agent definition
(``accounting_agents.assistant``) but are split out so the agent module
itself stays thin (name + instruction + tools list). Splitting the tools
is a structural cleanup — every function here is a pure read or a
gated write that operates on session state; the runner layer owns all
data fetching.

The package currently hosts the new P1 diagnostic / introspection
tools. The read / explain / write tools still live in
``accounting_agents.assistant`` and are re-exported there. This is a
deliberate, incremental split: each module here moves further toward
``read`` / ``explain`` / ``write`` / ``introspect`` as the team has
time to land the larger refactor (see plan P5-split-assistant).
"""
from .introspect import (
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
