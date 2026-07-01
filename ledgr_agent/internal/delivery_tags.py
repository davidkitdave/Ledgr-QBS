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


def _date_from_numeric_reference(value: str) -> date | None:
    """Parse a leading ``YYYYMMDD`` run from receipt / POS numbers (e.g. ``202602020015``)."""
    if not value:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        parsed = datetime.strptime(digits[:8], "%Y%m%d").date()
    except ValueError:
        return None
    if 1990 <= parsed.year <= 2099:
        return parsed
    return None


def document_date_from_fields(document: dict[str, Any]) -> date | None:
    """Resolve a booking date; prefer receipt-number ``YYYYMMDD`` over misread timestamps."""
    ref = str(
        document.get("invoice_number") or document.get("reference") or ""
    ).strip()
    ref_date = _date_from_numeric_reference(ref)
    inv_raw = document.get("invoice_date")
    inv_date = parse_document_date(inv_raw)
    alt_date = parse_document_date(document.get("date"))

    if ref_date and inv_date and inv_date.year < ref_date.year - 1:
        return ref_date
    if ref_date and inv_date is None and "GMT" in str(inv_raw or ""):
        return ref_date
    return inv_date or alt_date or ref_date


def sanitize_outlier_document_dates(documents: list[dict[str, Any]]) -> None:
    """Snap obvious Gemini date outliers in multi-receipt bundles to the bundle median year."""
    if len(documents) < 3:
        return
    resolved: list[tuple[dict[str, Any], date]] = []
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        parsed = document_date_from_fields(doc)
        if parsed is not None:
            resolved.append((doc, parsed))
    if len(resolved) < 3:
        return
    years = sorted(d.year for _, d in resolved)
    median_year = years[len(years) // 2]
    for doc, parsed in resolved:
        if parsed.year >= median_year - 1:
            continue
        ref_date = _date_from_numeric_reference(
            str(doc.get("invoice_number") or doc.get("reference") or "")
        )
        if ref_date and ref_date.year >= median_year - 1:
            doc["invoice_date"] = ref_date.strftime("%Y-%m-%d")
            continue
        try:
            doc["invoice_date"] = parsed.replace(year=median_year).strftime("%Y-%m-%d")
        except ValueError:
            pass


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
    doc_date = document_date_from_fields(document) or date.today()
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
    doc_date = document_date_from_fields(document) or date.today()
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
