"""Credit visibility tools for ADK web / agents-cli (playground QA)."""

from __future__ import annotations

from typing import Any

from ledgr_agent.tools.document_tools import _get_credit_service


def _resolve_firm_id(state: Any) -> str | None:
    if state is None:
        return None
    getter = getattr(state, "get", None)
    if getter is None:
        return None
    for key in ("firm_id", "slack_team_id"):
        val = getter(key)
        if val and str(val).strip():
            return str(val).strip()
    return None


def read_credit_balance(tool_context: Any) -> dict[str, Any]:
    """Return the current credit balance for the active firm (workspace team id).

    Reads ``firm_id`` or ``slack_team_id`` from the ADK session state. In dev,
    seed credits with ``LEDGR_DEV_CREDIT_GRANTS=T_PLAYGROUND:50`` before starting
    ``adk web``.
    """

    state = getattr(tool_context, "state", None)
    if state is None:
        return {
            "status": "error",
            "message": "no session state — run inside ADK web or agents-cli",
        }

    firm_id = _resolve_firm_id(state)
    if not firm_id:
        return {
            "status": "error",
            "message": (
                "no firm_id in session. Set LEDGR_PLAYGROUND_FIRM_ID or add "
                "firm_id to playground_profile.json"
            ),
        }

    service = _get_credit_service()
    if service is None:
        return {"status": "error", "message": "credit service unavailable"}

    balance = int(service.read_balance(firm_id))
    return {
        "status": "success",
        "firm_id": firm_id,
        "balance": balance,
        "message": f"Balance: {balance} credits",
    }
