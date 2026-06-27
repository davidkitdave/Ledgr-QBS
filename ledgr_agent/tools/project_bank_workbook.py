"""Map a read-bank-statement JSON dict into workbook sheets (light path)."""

from __future__ import annotations

from typing import Any

from ledgr_agent.export.bank_workbook import build_bank_workbook


def project_bank_workbook(
    tool_context: Any,
    statement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project *statement* (from ``read_bank_statement``) into workbook sheets.

    Args:
        tool_context: ADK session context; falls back to ``read_bank_statement`` state.
        statement: Plain dict with an ``accounts`` list.

    Returns:
        ``{status, sheet_count, sheets}`` where each sheet has title, columns, rows,
        reconciled, and reconcile_note.
    """
    if (not statement or not isinstance(statement, dict)) and tool_context is not None:
        state = getattr(tool_context, "state", None)
        if state is not None:
            cached = state.get("read_bank_statement")
            if isinstance(cached, dict):
                statement = cached
    if not statement or not isinstance(statement, dict):
        return {"status": "error", "message": "statement must be a non-empty dict"}
    accounts = statement.get("accounts") or []
    if not accounts:
        return {"status": "error", "message": "statement.accounts is required and must not be empty"}

    extract_mode = None
    meta = statement.get("extraction_meta")
    if isinstance(meta, dict):
        extract_mode = meta.get("extract_mode")

    payload = build_bank_workbook(statement, extract_mode=extract_mode)
    result = {"status": "success", **payload}
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        tool_context.state["bank_workbook"] = result
    return result
