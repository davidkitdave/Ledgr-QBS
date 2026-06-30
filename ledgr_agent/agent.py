from __future__ import annotations

import logging
import os

from google.adk.agents import Agent

from ledgr_agent.billing import read_credit_balance, wire_playground_credits
from ledgr_agent.internal.gemini import lite_model
from ledgr_agent.tools.build_sheets import build_sheets
from ledgr_agent.tools.read_doc import read_doc

_log = logging.getLogger(__name__)


def _seed_enabled() -> bool:
    if (os.environ.get("LEDGR_ENV") or "dev").strip().lower() == "prod":
        return False
    raw = os.environ.get("LEDGR_PLAYGROUND_SEED", "")
    return raw.strip().lower() not in {"false", "0", "no", "off"}


def seed_playground_profile_if_needed(state: dict) -> bool:
    """Inject a lightweight playground profile into session state when empty."""
    if state is None:
        return False
    if state.get("client_id") is not None or state.get("client_name") is not None:
        return False
    if not _seed_enabled():
        return False

    state.update(
        {
            "client_id": os.environ.get("LEDGR_PLAYGROUND_CLIENT_ID", "playground"),
            "client_name": os.environ.get("LEDGR_PLAYGROUND_CLIENT_NAME", "Playground Client"),
            "region": os.environ.get("LEDGR_PLAYGROUND_REGION", "SINGAPORE"),
            "software": os.environ.get("LEDGR_PLAYGROUND_SOFTWARE", "qbs"),
            "base_currency": os.environ.get("LEDGR_PLAYGROUND_CURRENCY", "SGD"),
            "tax_registered": True,
            "fye_month": 12,
            "ledger_data": [],
            "ledger_row_count": 0,
            "fy_loaded": "none",
            "fy_pointers": [],
            "processing_log": [],
            "pending_reviews": [],
        }
    )
    _log.info(
        "ledgr_agent: playground profile seeded (client_id=%s)",
        state.get("client_id"),
    )
    return True


def _before_agent_callback(callback_context) -> None:
    try:
        wire_playground_credits()
    except Exception:  # noqa: BLE001 — playground must never abort
        _log.debug("ledgr_agent: credit wire skipped", exc_info=True)
    state = getattr(callback_context, "state", None)
    if state is not None:
        try:
            if seed_playground_profile_if_needed(state):
                _log.info(
                    "ledgr_agent: playground profile seeded (client_id=%s)",
                    state.get("client_id"),
                )
        except Exception as exc:  # pragma: no cover
            _log.warning("ledgr_agent: playground seed failed (ignored): %s", exc)
    return None


root_agent = Agent(
    name="root_accountant_agent",
    model=lite_model(),
    description="Lean Ledgr accountant agent: read documents and build workbook rows.",
    instruction=(
        "You are the Ledgr accountant agent. "
        "When the user uploads a file: you MUST call read_doc with paths=[] "
        "then build_sheets and summarize the rows. "
        "Use read_credit_balance when the user asks about credits, balance, or billing. "
        "Never invent document fields — only use tool output. "
        "For playground or agents-cli --file uploads, pass paths=[] so the tool recovers the attachment. "
        "Do not invent placeholder paths such as invoice.png. "
        "Explain tax treatment only from processed results already in session state — never invent tax codes. "
        "After using any tool, you MUST always reply with a short plain-text summary. "
        "Never end your turn with only a tool call and no text."
    ),
    tools=[read_doc, build_sheets, read_credit_balance],
    before_agent_callback=_before_agent_callback,
)
