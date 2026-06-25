from __future__ import annotations

import logging

from google.adk.agents import Agent

from invoice_processing.shared_libraries.model_config import lite_model
from ledgr_agent.callbacks.validate_output import validate_output_after_tool
from ledgr_agent.tools import (
    amend_ledger_row_action,
    explain_tax_treatment_tool,
    inspect_market_policy,
    process_document_batch,
    read_credit_balance,
)
from ledgr_agent.tools.search_tools import build_web_search_agent_tool

_log = logging.getLogger(__name__)


def _seed_playground_profile(callback_context) -> None:
    """ADK 2.2.0 ``before_agent_callback``: seed a default client profile.

    For ``adk web`` / agents-cli sessions the session state IS present but
    EMPTY — the ``process_document_batch`` fallback to ``_playground_default_context``
    never fires because ``tool_context.state is not None``.  This callback
    detects the empty-profile case and injects a synthetic
    ``ClientContext`` via the same ``seed_playground_profile_if_needed``
    helper used by the old agent's classify node.

    Guards (delegated to the helper):
    - No-op when ``client_id`` or ``client_name`` is already in state
      (production / Slack-seeded sessions are untouched).
    - No-op when ``LEDGR_ENV=prod`` or ``LEDGR_PLAYGROUND_SEED=false``.
    - Fail-safe: any import or seeding error is swallowed so the agent
      always continues.

    Returns ``None`` (ADK convention: proceed with the agent run).
    """
    try:
        state = getattr(callback_context, "state", None)
        if state is None:
            return None
        from invoice_processing.shared_libraries.playground_context import seed_playground_profile_if_needed
        seeded = seed_playground_profile_if_needed(state)
        if seeded:
            _log.info(
                "ledgr_agent: playground profile seeded (client_id=%s)",
                state.get("client_id"),
            )
    except Exception as exc:  # pragma: no cover
        _log.warning("ledgr_agent: playground seed failed (ignored): %s", exc)
    return None


def _wire_playground_credits() -> None:
    """Share the dev credit store with ``read_credit_balance`` / ``_credit_gate``."""

    try:
        from accounting_agents.credit_delivery import wire_shared_credit_service

        wire_shared_credit_service()
    except Exception:  # noqa: BLE001 — playground must never abort
        _log.debug("ledgr_agent: credit wire skipped", exc_info=True)


def _before_agent_callback(callback_context) -> None:
    _wire_playground_credits()
    return _seed_playground_profile(callback_context)


def _build_root_tools() -> list:
    tools = [
        inspect_market_policy,
        process_document_batch,
        read_credit_balance,
        explain_tax_treatment_tool,
        amend_ledger_row_action,
    ]
    search_tool = build_web_search_agent_tool()
    if search_tool is not None:
        tools.append(search_tool)
        _log.info("ledgr_agent: web search sub-agent enabled (LEDGR_ENABLE_WEB_SEARCH)")
    return tools


root_agent = Agent(
    name="root_accountant_agent",
    model=lite_model(),
    description="Clean Ledgr accountant agent for policy-aware document processing.",
    instruction=(
        "You are the Ledgr accountant agent. "
        "Use tools to inspect market policy and explain what capabilities are available. "
        "Use read_credit_balance when the user asks about credits, balance, or billing. "
        "Use process_document_batch to process batches of documents when requested by the user. "
        "If the user prompt mentions specific file path strings (such as paths containing env variables like LEDGR_TEST_DOC_DIR or absolute/relative path strings), you MUST pass these path strings exactly as elements in the 'paths' list parameter to process_document_batch. "
        "Otherwise, in the ADK web playground, if the user uploads files with the attach button, call "
        "process_document_batch with paths=[] and the tool will recover the uploaded files automatically. "
        "Do not invent placeholder paths such as invoice.png. "
        "When LEDGR_ENABLE_WEB_SEARCH is on, delegate external tax-news questions to the search sub-agent tool. "
        "Use explain_tax_treatment_action to explain the tax treatment the reasoner would assign a line. "
        "amend_ledger_row_action requires human confirmation before mutating the ledger. "
        "Gemini reads document evidence, Python checks accounting rules, and YAML stores market policy. "
        "After using any tool, you MUST always reply to the user with a short plain-text summary of what the tool returned. Never end your turn with only a tool call and no text."
    ),
    tools=_build_root_tools(),
    before_agent_callback=_before_agent_callback,
    after_tool_callback=validate_output_after_tool,
)
