"""Map ``session.workbook`` to Slack FY workbook delivery."""

from __future__ import annotations

from typing import Any

from ledgr_slack.ledger_doc_identity import ledger_doc_identity
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
        software_key = normalize_system_key(software)
        targets = [s for s in sheets if s.get("system") == software_key] or sheets
        for index, sheet in enumerate(targets):
            rows = list(sheet.get("rows") or [])
            if not rows:
                continue
            sheet_name = str(sheet.get("title") or delivery.get("sheet") or "Purchase")
            identity = (
                sheet.get("invoice_number")
                or invoice_number_from_row(rows[0])
                or f"{source_filename}#{index}"
            )
            batch_fy = str(sheet.get("fy") or delivery.get("fy") or "unknown")
            batches.append(
                {
                    "sheet": sheet_name,
                    "doc_key": ledger_doc_identity(sheet_name, str(identity), index=index),
                    "rows": rows,
                    "fy": batch_fy,
                    "doc_type": sheet.get("doc_type") or delivery.get("doc_type") or "purchase",
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
    """Short human summary for the Slack delivery card.

    Mirrors the legacy ``nodes.compose_delivery_summary`` phrasing so the
    delivery card reads the same on the lean path: an "📒 Added N line(s)/
    transaction(s) … to your FY{fy} ledger." sentence carrying the row count,
    document count, and FY pointer.
    """
    kind = payload.get("kind") or "invoice"
    fy = payload.get("fy") or "unknown"
    batches = payload.get("batches") or []
    row_count = sum(len(batch.get("rows") or []) for batch in batches)
    if kind == "bank":
        return (
            f"📒 Added {row_count} transaction{'s' if row_count != 1 else ''} "
            f"to your FY{fy} bank statement."
        )
    doc_count = len(batches)
    return (
        f"📒 Added {row_count} line{'s' if row_count != 1 else ''} from "
        f"{doc_count} document{'s' if doc_count != 1 else ''} "
        f"to your FY{fy} ledger."
    )


def workbook_from_session_state(state: dict[str, Any]) -> dict[str, Any] | None:
    workbook = state.get(WORKBOOK_STATE_KEY)
    return workbook if isinstance(workbook, dict) else None
