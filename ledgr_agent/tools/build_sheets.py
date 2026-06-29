"""Build unified workbook sheets from ``read_doc`` session state."""

from __future__ import annotations

from typing import Any

from ledgr_agent.billing import billable_units, charge as billing_charge
from ledgr_agent.internal.delivery_tags import build_delivery_tags
from ledgr_agent.internal.export import DEFAULT_SYSTEMS, build_bank_workbook, project
from ledgr_agent.internal.skill_profiles import normalize_system_key
from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY

WORKBOOK_STATE_KEY = "workbook"


def _erp_sheets_from_document(document: dict[str, Any], systems: list[str]) -> list[dict[str, Any]]:
    projected = project(document, systems)
    sheets: list[dict[str, Any]] = []
    for system_key, result in (projected.get("results") or {}).items():
        sheets.append(
            {
                "title": result.get("sheet") or system_key,
                "columns": list(result.get("columns") or []),
                "rows": list(result.get("rows") or []),
                "system": system_key,
                "software_name": result.get("software_name"),
                "invoice_number": document.get("invoice_number") or "",
            }
        )
    return sheets


def _bank_sheets_from_statement(statement: dict[str, Any]) -> list[dict[str, Any]]:
    extract_mode = None
    meta = statement.get("extraction_meta")
    if isinstance(meta, dict):
        extract_mode = meta.get("extract_mode")
    built = build_bank_workbook(statement, extract_mode=extract_mode)
    return list(built.get("sheets") or [])


def _resolve_credit_units(read_payload: dict[str, Any]) -> int:
    stored = read_payload.get("credit_units")
    if stored is not None:
        return max(int(stored), 1)
    file_kind = read_payload.get("file_kind") or "commercial_documents"
    page_count = int(read_payload.get("page_count") or 1)
    document_count = int(read_payload.get("document_count") or len(read_payload.get("documents") or []))
    return billable_units(
        file_kind=file_kind,
        page_count=page_count,
        document_count=document_count,
    )


def build_sheets(
    tool_context: Any,
    systems: list[str] | None = None,
) -> dict[str, Any]:
    """Build workbook rows from the last ``read_doc`` result in session state.

    Uses ``file_kind`` from the read payload:
    - ``commercial_documents`` → ERP import sheets (QBS, Xero, AutoCount, SQL Account)
    - ``bank_statement`` → bank workbook tabs

    Args:
        tool_context: ADK session context; reads ``read_doc`` from state.
        systems: ERP keys for commercial documents (default: all four ERPs).

    Returns:
        ``{status, file_kind, sheet_count, sheets, credits}`` and stores
        the full payload under session key ``workbook``.
    """
    state = getattr(tool_context, "state", None) if tool_context is not None else None
    if state is None:
        return {"status": "error", "message": "no session state — call read_doc first"}

    read_payload = state.get(READ_DOC_STATE_KEY)
    if not isinstance(read_payload, dict):
        return {"status": "error", "message": "read_doc not in session — call read_doc first"}

    file_kind = read_payload.get("file_kind") or "commercial_documents"
    source_path = str(read_payload.get("source_path") or "document")
    credit_units = _resolve_credit_units(read_payload)
    charge_kind = "bank" if file_kind == "bank_statement" else "bill"
    target_systems: list[str] = []

    if file_kind == "bank_statement":
        accounts = read_payload.get("accounts") or []
        if not accounts:
            return {"status": "error", "message": "read_doc has no bank accounts"}
        sheets = _bank_sheets_from_statement(read_payload)
    else:
        documents = read_payload.get("documents") or []
        if not documents:
            return {"status": "error", "message": "read_doc has no commercial documents"}
        profile_software = state.get("software")
        if systems:
            target_systems = systems
        elif profile_software:
            target_systems = [normalize_system_key(str(profile_software))]
        else:
            target_systems = list(DEFAULT_SYSTEMS)
        sheets = []
        for document in documents:
            doc = dict(document)
            doc.setdefault("source_path", source_path)
            if not doc.get("lines"):
                return {
                    "status": "error",
                    "message": "document.lines is required and must not be empty",
                }
            sheets.extend(_erp_sheets_from_document(doc, target_systems))

    credits = billing_charge(
        tool_context,
        units=credit_units,
        file_id=source_path,
        kind=charge_kind,
    )

    delivery = build_delivery_tags(
        read_payload=read_payload,
        sheets=sheets,
        state=dict(state),
        source_path=source_path,
        file_kind=file_kind,
    )
    result: dict[str, Any] = {
        "status": "success",
        "file_kind": file_kind,
        "sheet_count": len(sheets),
        "sheets": sheets,
        "delivery": delivery,
        "credits": credits.model_dump(),
    }
    if file_kind == "commercial_documents":
        result["systems"] = target_systems
    state[WORKBOOK_STATE_KEY] = result
    return result
