"""Explain / reasoning tools for the chat assistant."""

from __future__ import annotations

import json
from datetime import date

from google.adk.tools import ToolContext

from invoice_processing.export.categorizer import resolve_account
from invoice_processing.export.client_context import (
    category_mapping_from_state,
    coa_from_state,
    entity_memory_from_state,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice

from accounting_agents.jurisdiction import resolve_jurisdiction, write_to_state
from accounting_agents.tax_reasoning import reason_one_invoice as _reason_one_invoice

from ..constants import PROCESSING_LOG_KEY
from ._helpers import (
    _build_resolver_state,
    _categorization_reason,
    _get_rows,
    _parse_bool_param,
    _parse_row_date,
    _to_float,
)

def explain_categorization(
    tool_context: ToolContext,
    vendor_name: str = "",
    line_description: str = "",
    category: str = "",
    row_index: str = "",
) -> str:
    """Explain why a line would map to a COA account using the engine's categorizer.

    Re-runs the same deterministic ``resolve_account`` logic the document pipeline
    uses (entity_memory → category_mapping → COA keyword). Does NOT call the LLM
    fallback — this explains the first-pass deterministic path only.

    Prefer ``row_index`` from a prior ``lookup_row`` hit — vendor and description
    are read from the ledger row automatically.

    Args:
        tool_context: Injected by ADK; provides session state.
        vendor_name: Supplier / vendor name on the invoice line.
        line_description: The line item description.
        category: Optional universal category label (for category_mapping lookups).
        row_index: Optional ledger row index from ``lookup_row`` (overrides vendor/description).

    Returns:
        JSON with ``status``, ``account_code``, ``account_name``, ``confidence``,
        ``source``, ``flagged``, and ``reason``.
    """
    try:
        state = tool_context.state
    except Exception:  # noqa: BLE001
        state = {}

    vendor = (vendor_name or "").strip()
    desc = (line_description or "").strip()
    if row_index:
        try:
            idx = int(str(row_index).strip())
            rows = _get_rows(tool_context)
            if 0 <= idx < len(rows):
                row = rows[idx]
                vendor = vendor or str(row.get("Vendor") or row.get("*ContactName") or "")
                desc = desc or str(row.get("Description") or row.get("*Description") or "")
        except (TypeError, ValueError):
            pass

    if not vendor and not desc:
        return json.dumps(
            {
                "status": "error",
                "message": "Need row_index from lookup_row or vendor_name + line_description.",
            },
            ensure_ascii=False,
        )

    res = resolve_account(
        desc,
        vendor,
        coa=coa_from_state(state),
        category_mapping=category_mapping_from_state(state),
        entity_memory=entity_memory_from_state(state),
        category=(category or "").strip() or None,
    )
    status = "unresolved" if res.source == "unresolved" else "resolved"
    return json.dumps(
        {
            "status": status,
            "account_code": res.account_code,
            "account_name": res.account_name,
            "confidence": res.confidence,
            "source": res.source,
            "flagged": res.flagged,
            "reason": _categorization_reason(res.source, res),
        },
        ensure_ascii=False,
    )
def explain_tax_treatment(
    tool_context: ToolContext,
    line_description: str,
    tax_keyword: str = "",
    net_amount: str = "",
    gst_amount: str = "",
    doc_type: str = "purchase",
    invoice_date: str = "",
    our_gst_registered: str = "",
) -> str:
    """Explain why a line gets a tax treatment code using the LLM tax reasoner.

    Thin wrapper over :func:`accounting_agents.tax_reasoning.reason_one_invoice`.
    Builds a one-line ``NormalizedInvoice`` in memory, resolves the active
    jurisdiction from session state (region + currency + counterparty
    country), and asks the LLM to reason about the line's treatment. The
    jurisdiction-aware reference YAML (``sg_gst.yaml`` / ``my_sst.yaml``) is
    surfaced to the LLM as rate-band context; Python only does the math
    guard. For Malaysia SST a 4.81 / 60.19 line resolves to SR + 8% (not
    the previous SG 9% mismatch that flagged a past MY receipt).

    Args:
        tool_context: Injected by ADK; provides session state.
        line_description: Line item description.
        tax_keyword: Explicit per-line tax wording from extraction (canonical field).
        net_amount: Line net amount ex-tax (canonical ``net_amount``).
        gst_amount: GST on the line (canonical ``gst_amount``).
        doc_type: ``purchase`` or ``sales``.
        invoice_date: ISO date ``YYYY-MM-DD`` (or empty for today-unknown).
        our_gst_registered: ``true``/``false``; empty → read ``state["tax_registered"]``.

    Returns:
        JSON with ``tax_treatment``, ``tax_confidence``, ``tax_flagged``,
        ``tax_reason``, ``tax_jurisdiction`` (so the chat agent can say which
        rule set decided the answer).
    """
    try:
        state = tool_context.state
    except Exception:  # noqa: BLE001
        state = {}

    reg = _parse_bool_param(our_gst_registered, default=bool(state.get("tax_registered", True)))
    if reg is None:
        reg = True

    inv_date: date | None = None
    if (invoice_date or "").strip():
        inv_date = _parse_row_date(invoice_date.strip())

    net = _to_float(net_amount) if (net_amount or "").strip() else None
    gst = _to_float(gst_amount) if (gst_amount or "").strip() else None
    dtype = (doc_type or "purchase").strip().lower()
    if dtype not in ("purchase", "sales"):
        dtype = "purchase"

    line = InvoiceLine(
        description=line_description or "",
        tax_keyword=(tax_keyword or "").strip() or None,
        net_amount=net,
        gst_amount=gst,
    )
    inv = NormalizedInvoice(
        doc_type=dtype,
        invoice_date=inv_date,
        our_gst_registered=reg,
        lines=[line],
    )
    # Resolve jurisdiction from state (NOT hardcoded SG) so the LLM tax
    # reasoner picks the right rate band.
    resolver_state = _build_resolver_state(state)
    resolution = resolve_jurisdiction(resolver_state)
    write_to_state(resolver_state, resolution)
    outcome = _reason_one_invoice(inv, state=resolver_state, jurisdiction_resolution=resolution)
    return json.dumps(
        {
            "tax_treatment": line.tax_treatment,
            "tax_confidence": line.tax_confidence,
            "tax_flagged": line.tax_flagged,
            "tax_reason": line.tax_reason,
            "tax_jurisdiction": resolution.jurisdiction.code,
            "tax_system": resolution.jurisdiction.tax_system,
            "used_llm": outcome.used_llm,
            "used_fallback": outcome.used_fallback,
        },
        ensure_ascii=False,
    )
def explain_document_processing(
    tool_context: ToolContext,
    filename: str = "",
    file_id: str = "",
) -> str:
    """Explain which extraction pipeline processed a filed document.

    Reads the ``processing_log`` injected by the Slack runner. Use when the user
    asks whether SOA vs invoice routing was correct, or understand vs legacy path.

    Args:
        tool_context: Injected by ADK; provides session state.
        filename: Optional source filename to look up (case-insensitive).
        file_id: Optional Slack file id (takes precedence over filename).

    Returns:
        JSON with ``doc_type``, ``extraction_path``, ``soa_legacy_path``,
        ``row_count``, ``delivered_at``, and a short ``summary`` string.
    """
    raw_log = tool_context.state.get(PROCESSING_LOG_KEY) or []
    if not isinstance(raw_log, list) or not raw_log:
        return (
            "No processing history is loaded for this channel yet. "
            "Drop a document first, or ask after a delivery completes."
        )

    needle_file = filename.strip().lower()
    needle_id = file_id.strip()
    match: dict | None = None
    if needle_id:
        for entry in raw_log:
            if isinstance(entry, dict) and str(entry.get("file_id") or "") == needle_id:
                match = entry
                break
    elif needle_file:
        for entry in raw_log:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("filename") or "").strip().lower() == needle_file:
                match = entry
                break
    else:
        match = raw_log[0] if isinstance(raw_log[0], dict) else None

    if not match:
        return json.dumps(
            {
                "error": "not_found",
                "message": "No matching delivery in the recent processing log.",
                "recent": [
                    {
                        "filename": e.get("filename"),
                        "file_id": e.get("file_id"),
                        "doc_type": e.get("doc_type"),
                        "extraction_path": e.get("extraction_path"),
                    }
                    for e in raw_log[:5]
                    if isinstance(e, dict)
                ],
            },
            ensure_ascii=False,
        )

    doc_type = str(match.get("doc_type") or "unknown")
    path = str(match.get("extraction_path") or "unknown")
    soa_legacy = bool(match.get("soa_legacy_path"))
    if doc_type == "statement_of_account" or soa_legacy:
        pipeline_note = (
            "This document used the legacy DocumentRecord path (required for SOA "
            "packages and complex multi-doc splits per ADR-0011)."
        )
    elif path == "understand":
        pipeline_note = (
            "This document used the Understand single-call extraction path "
            "(Drive-style summary + ledger lines)."
        )
    else:
        pipeline_note = f"Extraction path recorded as {path!r}."

    return json.dumps(
        {
            "filename": match.get("filename"),
            "file_id": match.get("file_id"),
            "doc_type": doc_type,
            "extraction_path": path,
            "soa_legacy_path": soa_legacy,
            "row_count": match.get("row_count"),
            "delivered_at": match.get("delivered_at"),
            "fy": match.get("fy"),
            "summary": pipeline_note,
        },
        ensure_ascii=False,
    )
