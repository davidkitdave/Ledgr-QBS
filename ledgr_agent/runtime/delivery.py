"""Map ``session.workbook`` to Slack FY workbook delivery."""

from __future__ import annotations

from typing import Any

from accounting_agents.ledger_doc_identity import ledger_doc_identity
from ledgr_agent.internal.skill_profiles import normalize_system_key
from ledgr_agent.tools.build_sheets import WORKBOOK_STATE_KEY

_INVOICE_NUMBER_HEADERS = (
    "Invoice Number",
    "*InvoiceNumber",
    "supplier_invoice_no",
    "invoice_number",
)


def invoice_number_from_row(row: dict[str, Any]) -> str:
    for header in _INVOICE_NUMBER_HEADERS:
        value = row.get(header)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _pick_client_sheet(sheets: list[dict[str, Any]], software: str) -> dict[str, Any] | None:
    if not sheets:
        return None
    software_key = normalize_system_key(software)
    for sheet in sheets:
        if sheet.get("system") == software_key:
            return sheet
    return sheets[0]


def workbook_to_ledger_payload(
    workbook: dict[str, Any],
    *,
    client_id: str,
    client_name: str,
    software: str,
    file_id: str,
    source_filename: str,
) -> dict[str, Any]:
    """Build ``ledger_rows``-shaped payload for :class:`SlackLedgerStore`."""
    delivery = dict(workbook.get("delivery") or {})
    fy = str(delivery.get("fy") or "unknown")
    kind = str(delivery.get("kind") or "invoice")
    sheets = list(workbook.get("sheets") or [])
    batches: list[dict[str, Any]] = []

    if kind == "bank":
        for index, sheet in enumerate(sheets):
            title = str(sheet.get("title") or f"Bank-{index}")
            batches.append(
                {
                    "sheet": title,
                    "doc_key": ledger_doc_identity(title, title, index=index),
                    "rows": list(sheet.get("rows") or []),
                }
            )
    else:
        target = _pick_client_sheet(sheets, software)
        if target is not None:
            sheet_name = str(target.get("title") or delivery.get("sheet") or "Purchase")
            rows = list(target.get("rows") or [])
            identity = delivery.get("invoice_number") or invoice_number_from_row(rows[0] if rows else {})
            if not identity:
                identity = source_filename
            batches.append(
                {
                    "sheet": sheet_name,
                    "doc_key": ledger_doc_identity(sheet_name, str(identity)),
                    "rows": rows,
                }
            )

    return {
        "client_id": client_id,
        "client_name": client_name,
        "fy": fy,
        "kind": kind,
        "software": software,
        "doc_type": delivery.get("doc_type") or "purchase",
        "file_id": file_id,
        "source_filename": source_filename,
        "delivered": True,
        "batches": batches,
    }


def compose_delivery_summary(workbook: dict[str, Any], payload: dict[str, Any]) -> str:
    """Short human summary for the Slack delivery card."""
    delivery = workbook.get("delivery") or {}
    kind = payload.get("kind") or "invoice"
    fy = payload.get("fy") or "unknown"
    batches = payload.get("batches") or []
    row_count = sum(len(batch.get("rows") or []) for batch in batches)
    source = delivery.get("source_filename") or payload.get("source_filename") or "document"
    if kind == "bank":
        return f"Added {row_count} bank line(s) from `{source}` to FY{fy} workbook."
    inv = delivery.get("invoice_number") or ""
    inv_part = f" ({inv})" if inv else ""
    sheet = delivery.get("sheet") or (batches[0].get("sheet") if batches else "Purchase")
    return (
        f"Added {row_count} row(s) from `{source}`{inv_part} "
        f"to FY{fy} {sheet} sheet."
    )


def workbook_from_session_state(state: dict[str, Any]) -> dict[str, Any] | None:
    workbook = state.get(WORKBOOK_STATE_KEY)
    return workbook if isinstance(workbook, dict) else None
