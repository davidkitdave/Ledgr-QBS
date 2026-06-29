"""HITL helpers for the clean-agent Slack path (ADR-0026 / Stream D.4)."""

from __future__ import annotations

from typing import Any

CLEAN_AGENT_HITL_KIND = "clean_agent_batch"

_ACCOUNT_CODE_HEADERS = (
    "Account",
    "AccountCode",
    "*AccountCode",
    "AccNo",
    "_ACCOUNT(10)",
)


def _account_code_from_row(row: dict[str, Any]) -> str:
    for header in _ACCOUNT_CODE_HEADERS:
        if header in row:
            return str(row.get(header) or "")
    return ""


def _set_account_code_on_row(row: dict[str, Any], account_code: str) -> None:
    for header in _ACCOUNT_CODE_HEADERS:
        if header in row:
            row[header] = account_code
            return


def op_id_for_file(channel_id: str, file_id: str) -> str:
    """Stable interrupt id — matches ``nodes._approval_interrupt_id`` convention."""
    return f"{channel_id}:{file_id}"


def should_pause_for_hitl(batch: dict[str, Any]) -> bool:
    """Return whether Slack should post an Approve/Edit/Reject card."""

    status = str(batch.get("status") or "")
    if status in {"needs_review", "partial"}:
        return True
    review_requests = batch.get("review_requests") or []
    return any(str(item.get("severity") or "") == "hard_review" for item in review_requests)


def approval_summary_from_batch(batch: dict[str, Any]) -> str:
    """Build the approval-card prose from structured ``BatchResult`` reviews."""

    display_lines: list[str] = []
    for req in batch.get("review_requests") or []:
        message = str(req.get("message") or req.get("id") or "").strip()
        if message:
            display_lines.append(message)
    for warn in batch.get("soft_warnings") or []:
        message = str(warn.get("message") or "").strip()
        if message:
            display_lines.append(message)

    header = (
        "Please review the proposed accounting entries — the following need a "
        "human decision before they are added to the ledger:"
    )
    if not display_lines:
        display_lines = ["This document needs your review before it can be posted."]
    bullets = "\n".join(f"  • {line}" for line in display_lines)
    return f"{header}\n{bullets}"


def ledger_rows_to_edit_lines(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Map ledger export rows into the shape ``invoice_edit_modal`` expects."""

    lines: list[dict[str, Any]] = []
    for batch in payload.get("batches") or []:
        for row in batch.get("rows") or []:
            lines.append(
                {
                    "description": row.get("Description") or row.get("Line Description") or "",
                    "account_code": _account_code_from_row(row),
                    "tax_treatment": row.get("TaxType") or row.get("Tax Code") or "",
                    "net_amount": row.get("Amount") or row.get("Source Amount") or row.get("Net Amount"),
                }
            )
    return lines


def apply_edits_to_ledger_payload(payload: dict[str, Any], edits: dict[str, Any]) -> dict[str, Any]:
    """Apply modal line edits onto the stashed ledger payload (best-effort)."""

    updated = dict(payload)
    batches = []
    edit_lines = list((edits or {}).get("lines") or [])
    row_index = 0
    for batch in payload.get("batches") or []:
        new_rows = []
        for row in batch.get("rows") or []:
            row_copy = dict(row)
            matching = next((item for item in edit_lines if item.get("index") == row_index), None)
            if matching is not None:
                if matching.get("account_code") is not None:
                    _set_account_code_on_row(row_copy, matching["account_code"])
                if matching.get("tax_treatment") is not None:
                    if "TaxType" in row_copy:
                        row_copy["TaxType"] = matching["tax_treatment"]
                    elif "Tax Code" in row_copy:
                        row_copy["Tax Code"] = matching["tax_treatment"]
                if matching.get("net_amount") is not None:
                    for amount_key in ("Amount", "Source Amount", "Net Amount"):
                        if amount_key in row_copy:
                            row_copy[amount_key] = matching["net_amount"]
                            break
            new_rows.append(row_copy)
            row_index += 1
        batches.append({**batch, "rows": new_rows})
    updated["batches"] = batches
    return updated
