"""Build unified workbook sheets from ``read_doc`` session state."""

from __future__ import annotations

from typing import Any

from ledgr_agent.billing import (
    billable_units,
    charge as billing_charge,
    delivery_idempotency_key,
    get_shared_credit_service,
    resolve_firm_id,
)
from ledgr_agent.internal.schemas import CreditSummary
from ledgr_agent.internal.delivery_tags import (
    build_delivery_tags,
    document_sheet_meta,
    fye_month_from_state,
)
from ledgr_agent.internal.export import DEFAULT_SYSTEMS, build_bank_workbook, project
from ledgr_agent.internal.skill_profiles import normalize_system_key
from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY

WORKBOOK_STATE_KEY = "workbook"


def _plain_state(state: Any) -> dict[str, Any]:
    """ADK ``State`` supports ``__getitem__`` but not ``dict(state)`` — use ``to_dict()``."""
    if state is None:
        return {}
    if isinstance(state, dict):
        return dict(state)
    to_dict = getattr(state, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {}


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


def _postable_commercial_documents(read_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        doc
        for doc in (read_payload.get("documents") or [])
        if isinstance(doc, dict)
        and (doc.get("document_kind") or "").strip().lower() != "statement_of_account"
    ]


def _resolve_credit_units(read_payload: dict[str, Any]) -> int:
    stored = read_payload.get("credit_units")
    if stored is not None:
        return max(int(stored), 1)
    file_kind = read_payload.get("file_kind") or "commercial_documents"
    page_count = int(read_payload.get("page_count") or 1)
    if file_kind == "commercial_documents":
        document_count = len(_postable_commercial_documents(read_payload))
    else:
        document_count = int(
            read_payload.get("document_count") or len(read_payload.get("documents") or [])
        )
    return billable_units(
        file_kind=file_kind,
        page_count=page_count,
        document_count=document_count,
    )


def _estimate_credits(tool_context: Any, *, units: int) -> CreditSummary:
    firm_id = resolve_firm_id(tool_context)
    if not firm_id:
        return CreditSummary(
            credits_estimated=units,
            credits_used=0,
            credits_remaining=None,
            credit_status="not_billable",
        )
    balance = get_shared_credit_service().read_balance(firm_id)
    return CreditSummary(
        credits_estimated=units,
        credits_used=0,
        credits_remaining=balance,
        credit_status="estimated",
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
    state_plain = _plain_state(state)
    channel_id = str(state_plain.get("channel_id") or "")
    slack_file_id = str(state_plain.get("file_id") or "")
    if channel_id and slack_file_id:
        billing_file_id = delivery_idempotency_key(channel_id=channel_id, file_id=slack_file_id)
    else:
        billing_file_id = source_path
    credit_units = _resolve_credit_units(read_payload)
    charge_kind = "bank" if file_kind == "bank_statement" else "bill"
    target_systems: list[str] = []

    if file_kind == "bank_statement":
        accounts = read_payload.get("accounts") or []
        if not accounts:
            return {"status": "error", "message": "read_doc has no bank accounts"}
        sheets = _bank_sheets_from_statement(read_payload)
    else:
        documents = _postable_commercial_documents(read_payload)
        if not documents:
            return {
                "status": "error",
                "message": "read_doc has no postable commercial documents (only statement of account)",
            }
        profile_software = state.get("software")
        if systems:
            target_systems = systems
        elif profile_software:
            target_systems = [normalize_system_key(str(profile_software))]
        else:
            target_systems = list(DEFAULT_SYSTEMS)
        sheets = []
        fye_month = fye_month_from_state(state_plain)
        for document in documents:
            doc = dict(document)
            doc.setdefault("source_path", source_path)
            if not doc.get("lines"):
                continue
            doc_meta = document_sheet_meta(doc, fye_month=fye_month)
            for sheet in _erp_sheets_from_document(doc, target_systems):
                sheet.update(doc_meta)
                sheets.append(sheet)
        if not sheets:
            return {
                "status": "error",
                "message": "no commercial documents with line items to book",
            }

    defer_billing = bool(state_plain.get("defer_slack_delivery"))
    if defer_billing:
        credits = _estimate_credits(tool_context, units=credit_units)
    else:
        credits = billing_charge(
            tool_context,
            units=credit_units,
            file_id=billing_file_id,
            kind=charge_kind,
        )

    delivery = build_delivery_tags(
        read_payload=read_payload,
        sheets=sheets,
        state=_plain_state(state),
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
        result["documents_summary"] = [
            {
                "invoice_number": doc.get("invoice_number") or "",
                "vendor_name": doc.get("vendor_name") or "",
                "invoice_date": doc.get("invoice_date") or "",
                "currency": doc.get("currency") or "",
                "subtotal": doc.get("subtotal"),
                "tax_total": doc.get("tax_total"),
                "grand_total": doc.get("grand_total"),
                "document_kind": doc.get("document_kind") or "",
                "tax_breakdown": [
                    {
                        "tax_treatment": comp.get("tax_treatment") or "",
                        "tax_rate_percent": comp.get("tax_rate_percent"),
                        "taxable_amount": comp.get("taxable_amount"),
                        "tax_amount": comp.get("tax_amount"),
                    }
                    for comp in (doc.get("tax_breakdown") or [])
                ],
            }
            for doc in (read_payload.get("documents") or [])
            if (doc.get("document_kind") or "").strip().lower() != "statement_of_account"
        ]
    state[WORKBOOK_STATE_KEY] = result
    return result
