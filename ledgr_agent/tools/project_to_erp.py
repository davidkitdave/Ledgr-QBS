"""Map a read-document JSON dict into ERP import rows (light path)."""

from __future__ import annotations

from typing import Any

from ledgr_agent.export.erp_projection import DEFAULT_SYSTEMS, project


def project_to_erp(
    tool_context: Any,
    document: dict[str, Any],
    systems: list[str] | None = None,
) -> dict[str, Any]:
    """Project *document* (from ``read_document``) into ERP import rows.

    Args:
        tool_context: ADK session context (unused today; reserved for ``output_key`` fallback).
        document: Plain dict with header fields and a ``lines`` list.
        systems: ERP keys to project (default: qbs, xero, autocount, sql_account).

    Returns:
        ``{status, systems, results}`` where each result has software_name, sheet,
        columns, and rows.
    """
    if (not document or not isinstance(document, dict)) and tool_context is not None:
        state = getattr(tool_context, "state", None)
        if state is not None:
            cached = state.get("read_document")
            if isinstance(cached, dict):
                document = cached
    if not document or not isinstance(document, dict):
        return {"status": "error", "message": "document must be a non-empty dict"}
    if not document.get("lines"):
        return {"status": "error", "message": "document.lines is required and must not be empty"}
    payload = project(document, systems or list(DEFAULT_SYSTEMS))
    return {"status": "success", **payload}
