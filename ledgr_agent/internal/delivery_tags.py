"""Build Slack delivery metadata from read + sheet output."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from ledgr_agent.internal.fy import fy_for_date


def parse_document_date(value: Any) -> date | None:
    """Parse ISO or common printed date strings."""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def fye_month_from_state(state: dict[str, Any]) -> int:
    raw = state.get("fye_month")
    try:
        month = int(raw) if raw is not None else 12
    except (TypeError, ValueError):
        month = 12
    return month if 1 <= month <= 12 else 12


def _first_bank_date(read_payload: dict[str, Any]) -> date | None:
    for account in read_payload.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        for txn in account.get("transactions") or []:
            if not isinstance(txn, dict):
                continue
            parsed = parse_document_date(txn.get("date"))
            if parsed is not None:
                return parsed
    return None


def document_sheet_meta(document: dict[str, Any], *, fye_month: int) -> dict[str, Any]:
    """Per-document FY and sheet metadata for multi-document workbooks."""
    doc_type = str(document.get("doc_type") or "purchase").strip().lower()
    if doc_type not in ("purchase", "sales"):
        doc_type = "purchase"
    doc_date = parse_document_date(document.get("invoice_date")) or date.today()
    return {
        "fy": fy_for_date(doc_date, fye_month),
        "doc_type": doc_type,
        "invoice_number": str(document.get("invoice_number") or ""),
    }


def build_delivery_tags(
    *,
    read_payload: dict[str, Any],
    sheets: list[dict[str, Any]],
    state: dict[str, Any],
    source_path: str,
    file_kind: str,
) -> dict[str, Any]:
    """Return ``delivery`` block for ``session.workbook``."""
    fye_month = fye_month_from_state(state)
    source_filename = str(state.get("source_filename") or Path(source_path).name)

    if file_kind == "bank_statement":
        doc_date = _first_bank_date(read_payload) or date.today()
        sheet_title = str((sheets[0] or {}).get("title") or "Bank") if sheets else "Bank"
        return {
            "fy": fy_for_date(doc_date, fye_month),
            "kind": "bank",
            "doc_type": "bank_statement",
            "sheet": sheet_title,
            "invoice_number": "",
            "source_filename": source_filename,
        }

    documents = read_payload.get("documents") or []
    document = next((doc for doc in documents if doc.get("lines")), documents[0] if documents else {})
    doc_type = str(document.get("doc_type") or "purchase").strip().lower()
    if doc_type not in ("purchase", "sales"):
        doc_type = "purchase"
    doc_date = parse_document_date(document.get("invoice_date")) or date.today()
    sheet_title = "Sales" if doc_type == "sales" else "Purchase"
    if sheets:
        sheet_title = str(sheets[0].get("title") or sheet_title)

    return {
        "fy": fy_for_date(doc_date, fye_month),
        "kind": "invoice",
        "doc_type": doc_type,
        "sheet": sheet_title,
        "invoice_number": str(document.get("invoice_number") or ""),
        "source_filename": source_filename,
    }
