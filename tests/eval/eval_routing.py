"""Eval lane routing — maps golden eval case IDs to ADK agent modules.

Cluster B (chat trajectory) runs on the standalone ``assistant_agent`` via
``accounting_agents.chat_eval.agent``. All other clusters run on the document
coordinator graph via ``accounting_agents.agent``.

ADK ``AgentEvaluator`` requires a Python module path ending in ``.agent`` (or
a module with an ``agent`` member) that exposes ``root_agent``.
"""

from __future__ import annotations

CHAT_CASE_PREFIX = "B"
DOC_AGENT_MODULE = "accounting_agents.agent"
CHAT_AGENT_MODULE = "accounting_agents.chat_eval.agent"

CHAT_CASE_IDS = (
    "B3_chat_show_client_profile_trajectory",
    "B4_chat_summarize_by_category_trajectory",
    "B5_chat_multi_turn_fye_then_currency",
    "B6_chat_invoice_account_code_trajectory",
)


def is_chat_case(eval_case_id: str) -> bool:
    """Return True when *eval_case_id* belongs to the chat lane (cluster B)."""
    return eval_case_id.startswith(CHAT_CASE_PREFIX)


def agent_module_for_case(eval_case_id: str) -> str:
    """Return the ADK agent module path for a golden eval case."""
    if is_chat_case(eval_case_id):
        return CHAT_AGENT_MODULE
    return DOC_AGENT_MODULE


def chat_agent_directory() -> str:
    """Relative path to the chat eval agent directory (agents-cli single-agent)."""
    return "accounting_agents/chat_eval"
