from __future__ import annotations

import logging

from google.adk.agents import Agent

from ledgr_agent.shared.model_config import lite_model
from ledgr_agent.shared.playground_seed import seed_playground_profile_if_needed
from ledgr_agent.callbacks.validate_output import validate_output_after_tool
from ledgr_agent.tools import (
    amend_ledger_row_action,
    explain_tax_treatment_tool,
    extract_one_bill_minimal,
    inspect_market_policy,
    process_document_batch,
    project_to_erp,
    read_credit_balance,
    read_document,
    read_bank_statement,
    project_bank_workbook,
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


def _build_pipeline_agent_tools() -> list:
    from google.adk.tools.agent_tool import AgentTool

    from ledgr_agent.agents.bank_pipeline import build_bank_pipeline_agent
    from ledgr_agent.agents.bill_pipeline import build_bill_pipeline_agent

    return [
        AgentTool(agent=build_bill_pipeline_agent()),
        AgentTool(agent=build_bank_pipeline_agent()),
    ]


def _build_root_tools() -> list:
    tools = [
        inspect_market_policy,
        process_document_batch,
        extract_one_bill_minimal,
        *_build_pipeline_agent_tools(),
        read_document,
        project_to_erp,
        read_bank_statement,
        project_bank_workbook,
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
        "Document routing (pick one path per upload): "
        "(1) Single commercial bill (invoice, receipt, credit note) and user wants ERP import rows → "
        "bill_pipeline (preferred) or read_document then project_to_erp. "
        "Default ERPs: qbs, xero, autocount, sql_account. "
        "(2) Bank statement PDF → bank_pipeline (preferred) or read_bank_statement then project_bank_workbook. "
        "Do not call read_document on bank PDFs. "
        "(3) SOA, multi-invoice PDF, multi-file batch, COA/tax categorization, or credit-gated factory run → process_document_batch. "
        "(4) Fast single-bill extract with printed SR/ZR tax breakdown only → extract_one_bill_minimal. "
        "Never invent document fields — only use what read_document returns. Show ERP row summaries in your reply. "
        "If the user prompt mentions specific file path strings (LEDGR_TEST_DOC_DIR or absolute paths), pass them exactly in the tool paths list. "
        "For light-path uploads in playground or agents-cli --file, pass paths=[] so the tool recovers the attachment. "
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
