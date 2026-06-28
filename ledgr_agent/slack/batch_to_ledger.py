"""Map a ``process_document_batch`` ``BatchResult`` dict to Slack ledger delivery structs."""

from __future__ import annotations

import re
from typing import Any

from accounting_agents.ledger_doc_identity import (
    ledger_doc_identity,
    ledger_doc_key_for_export_row,
)
from invoice_processing.export.exporters import get_exporter

_INVOICE_NUMBER_HEADERS = (
    "Invoice Number",
    "*InvoiceNumber",
    "supplier_invoice_no",
    "invoice_number",
)

_FY_WORKBOOK_RE = re.compile(r"FY(\d{4})", re.IGNORECASE)


def _invoice_number_from_row(row: dict[str, Any]) -> str:
    for header in _INVOICE_NUMBER_HEADERS:
        value = row.get(header)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _fy_from_workbook_name(workbook: str) -> str | None:
    match = _FY_WORKBOOK_RE.search(workbook or "")
    if match:
        return match.group(1)
    return None


def _fy_from_export_rows(export_rows: list[dict[str, Any]]) -> str | None:
    for row in export_rows:
        workbook = str(row.get("workbook") or "")
        fy = _fy_from_workbook_name(workbook)
        if fy:
            return fy
    return None


#: Row keys that are eval/routing metadata, not ledger columns. Stripped before
#: appending to the human-facing Slack ledger so the live sheet is byte-identical
#: whether or not the row carries issue-#28 provenance tags.
_EXPORT_METADATA_KEYS = {
    "workbook",
    "sheet",
    "source_doc_id",
    "tax_treatment",
    "account_code",
    "direction",
}


def _strip_export_metadata(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if key not in _EXPORT_METADATA_KEYS
    }


def _kind_from_batch(batch: dict[str, Any]) -> str:
    for source in (batch.get("per_file") or []) + (batch.get("posted_documents") or []):
        doc_type = str(source.get("doc_type") or "").strip().lower()
        if doc_type in {"bank_statement", "bank"}:
            return "bank"
    return "invoice"


def _primary_doc_type(batch: dict[str, Any]) -> str:
    for source in batch.get("per_file") or []:
        doc_type = str(source.get("doc_type") or "").strip().lower()
        if doc_type:
            return doc_type
    return "invoice"


def _batches_from_export_rows(
    export_rows: list[dict[str, Any]],
    *,
    posted_documents: list[dict[str, Any]],
    kind: str,
    software: str = "",
) -> list[dict[str, Any]]:
    if not export_rows:
        return []

    posted_invoices = {
        str(doc.get("invoice_number") or "").strip()
        for doc in posted_documents
        if doc.get("invoice_number")
    }

    if kind == "invoice" and posted_invoices:
        rows = [
            row
            for row in export_rows
            if _invoice_number_from_row(row) in posted_invoices
        ]
    else:
        rows = list(export_rows)

    exporter = get_exporter(software) if software else None

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        sheet = str(row.get("sheet") or "Purchase")
        if kind == "bank":
            identity = sheet
        else:
            identity = _invoice_number_from_row(row) or f"i{index}"
        key = (sheet, identity)
        grouped.setdefault(key, []).append(_strip_export_metadata(row))

    batches: list[dict[str, Any]] = []
    for index, ((sheet, identity), batch_rows) in enumerate(sorted(grouped.items())):
        if exporter is not None and kind != "bank":
            doc_key = ledger_doc_key_for_export_row(
                exporter, sheet, batch_rows[0], index
            )
        else:
            doc_key = ledger_doc_identity(sheet, identity, index=index)
        batches.append(
            {
                "sheet": sheet,
                "doc_key": doc_key,
                "rows": batch_rows,
            }
        )
    return batches


def ledger_payload_from_batch_result(
    batch: dict[str, Any],
    *,
    client_id: str,
    client_name: str,
    software: str,
    file_id: str,
    source_filename: str = "",
    delivered: bool = True,
) -> dict[str, Any]:
    """Build a ``state[ledger_rows]`` payload from a serialized ``BatchResult``."""

    export_rows = list(batch.get("export_rows") or [])
    posted_documents = list(batch.get("posted_documents") or [])
    kind = _kind_from_batch(batch)
    fy = _fy_from_export_rows(export_rows) or "unknown"
    batches = _batches_from_export_rows(
        export_rows,
        posted_documents=posted_documents,
        kind=kind,
        software=software,
    )

    payload: dict[str, Any] = {
        "client_id": client_id,
        "client_name": client_name,
        "fy": fy,
        "kind": kind,
        "software": software,
        "doc_type": _primary_doc_type(batch),
        "file_id": file_id,
        "source_filename": source_filename,
        "delivered": delivered,
        "batches": batches,
    }
    if kind == "invoice":
        payload["extracted_doc_count"] = len(posted_documents) or len(batches)
    return payload
