"""Diagnostic / introspection tools (P1, 2026-06-16) — read state only, no I/O.

All four tools read session state populated by the Slack runner:
- ``diagnose_assistant_context`` — single JSON snapshot of FY / row count
  / processing-log / pending-review counts.
- ``list_processing_history`` — chronological browse of recent deliveries.
- ``get_document_processing_detail`` — per-file deep-dive that merges
  the processing log with the read-only per-document session snapshot.
- ``list_pending_reviews`` — HITL interrupts awaiting the user's approval.

The runner owns the data; the tools own the rendering. ADK-aligned:
function tools with precise docstrings + ``status`` fields.
"""

from __future__ import annotations

import json

from google.adk.tools import ToolContext

from accounting_agents.assistant_tools._helpers import filename_matches_query
from accounting_agents.assistant import (
    DOCUMENT_SESSIONS_KEY,
    LEDGER_DATA_KEY,
    PENDING_REVIEWS_KEY,
    PROCESSING_LOG_KEY,
    _diagnostic_counts,
    _get_rows,
    _parse_int_param,
)


def diagnose_assistant_context(tool_context: ToolContext) -> str:
    """Return a single JSON snapshot of everything the assistant knows right now.

    Call this **first** when the user asks an extraction/SOA/HITL question, the
    ledger looks empty, or before claiming "I cannot see" anything. Returns
    client + FY + row counts + processing-log depth + pending-review count so
    the LLM can answer "what do you actually have on file?" without guessing.

    Args:
        tool_context: Injected by ADK; provides session state.

    Returns:
        JSON ``{"status": "success", "client_name", "software", "fy_loaded",
        "ledger_row_count", "fy_pointers", "processing_log_count",
        "pending_review_count", "onboarding_required"}``.
    """
    diag = _diagnostic_counts(tool_context)
    pointers = tool_context.state.get("fy_pointers") or []
    if not isinstance(pointers, list):
        pointers = []
    ledger_type = "empty"
    if diag["ledger_row_count"]:
        # Cheap inference: bank rows have a ``_sheet`` outside of
        # ``{Purchase, Sales}``; otherwise it's invoice-shaped.
        try:
            sample = tool_context.state.get(LEDGER_DATA_KEY) or []
        except Exception:  # noqa: BLE001
            sample = []
        is_bank = False
        for row in sample[:5]:
            if not isinstance(row, dict):
                continue
            sheet = str(row.get("_sheet") or "").strip()
            if sheet and sheet not in ("Purchase", "Sales"):
                is_bank = True
                break
        ledger_type = "bank" if is_bank else "invoice"
    payload = {
        "status": "success",
        "client_name": diag["client_name"],
        "software": diag["software"],
        "fy_loaded": diag["fy_loaded"],
        "ledger_row_count": diag["ledger_row_count"],
        "ledger_type": ledger_type,
        "fy_pointers": pointers,
        "processing_log_count": diag["processing_log_count"],
        "pending_review_count": diag["pending_review_count"],
        "onboarding_required": not bool(diag["software"]),
    }
    return json.dumps(payload, ensure_ascii=False)


def list_processing_history(
    tool_context: ToolContext, limit: str = "10"
) -> str:
    """Browse the recent document-processing deliveries for this channel.

    Distinct from ``list_recent_documents`` (which is ledger-row-driven) and
    from ``explain_document_processing`` (which is per-file introspection).
    Use this when the user wants a chronological "what came in recently?"
    view — e.g. "show me the last 5 documents you processed".

    Args:
        tool_context: Injected by ADK; provides session state.
        limit: Maximum entries to return (default ``10``, max ``50``).

    Returns:
        JSON ``{"entries": [{filename, file_id, doc_type, extraction_path,
        delivered_at, fy, row_count}, ...]}`` — empty list when the log is
        empty.
    """
    raw_log = tool_context.state.get(PROCESSING_LOG_KEY) or []
    if not isinstance(raw_log, list):
        raw_log = []
    cap = _parse_int_param(limit, default=10, minimum=1, maximum=50)
    entries: list[dict] = []
    for entry in raw_log[:cap]:
        if not isinstance(entry, dict):
            continue
        entries.append(
            {
                "filename": entry.get("filename"),
                "file_id": entry.get("file_id"),
                "doc_type": entry.get("doc_type"),
                "extraction_path": entry.get("extraction_path"),
                "soa_legacy_path": bool(entry.get("soa_legacy_path")),
                "delivered_at": entry.get("delivered_at"),
                "fy": entry.get("fy"),
                "row_count": entry.get("row_count"),
            }
        )
    return json.dumps({"entries": entries}, ensure_ascii=False)


def get_document_processing_detail(
    tool_context: ToolContext,
    file_id: str = "",
    filename: str = "",
) -> str:
    """Return a single document's processing details (delivery + session snapshot).

    Merges the matching entry from ``processing_log`` (Firestore-side
    delivery metadata) with the read-only session snapshot under
    ``document_sessions`` (the per-file ADK session state captured at chat
    time). Use this for "how was invoice.pdf extracted?" or "what did the
    reviewer flag on this doc?".

    Args:
        tool_context: Injected by ADK; provides session state.
        file_id: Slack file id (takes precedence over filename).
        filename: Source filename to look up (case-insensitive).

    Returns:
        JSON with the merged record, or a structured ``not_found`` payload
        listing the most recent deliveries so the LLM can name a candidate.
    """
    raw_log = tool_context.state.get(PROCESSING_LOG_KEY) or []
    if not isinstance(raw_log, list):
        raw_log = []
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
            stored = str(entry.get("filename") or "")
            if filename_matches_query(needle_file, stored):
                match = entry
                break
    if not match:
        return json.dumps(
            {
                "status": "not_found",
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

    # Layer in any read-only session snapshot.
    sessions = tool_context.state.get(DOCUMENT_SESSIONS_KEY) or {}
    snapshot: dict = {}
    if isinstance(sessions, dict):
        snap = sessions.get(str(match.get("file_id") or "")) or sessions.get(
            str(match.get("filename") or "").lower()
        )
        if isinstance(snap, dict):
            snapshot = snap

    merged = dict(match)
    if snapshot:
        merged["doc_type"] = snapshot.get("doc_type") or merged.get("doc_type")
        merged["extraction_path"] = (
            snapshot.get("extraction_path") or merged.get("extraction_path")
        )
        merged["review_reasons"] = snapshot.get("review_reasons") or []
        merged["source_filename"] = snapshot.get("source_filename") or merged.get(
            "filename"
        )
        merged["summary_table_size"] = snapshot.get("summary_table_size")
        merged["normalized_invoice_count"] = snapshot.get(
            "normalized_invoice_count"
        )
    return json.dumps(merged, ensure_ascii=False, default=str)


def list_pending_reviews(tool_context: ToolContext) -> str:
    """List the HITL interrupts awaiting the user's approval for this channel.

    Reads ``pending_reviews`` from session state (injected by the Slack
    runner from ``hitl.list_pending_interrupts``). Use when the user asks
    "anything waiting for me?" or "what needs my approval?".

    Args:
        tool_context: Injected by ADK; provides session state.

    Returns:
        JSON ``{"reviews": [{interrupt_id, file_id, filename, doc_type,
        asked_at, reason, options}]}`` — empty list when nothing is pending.
    """
    raw = tool_context.state.get(PENDING_REVIEWS_KEY) or []
    if not isinstance(raw, list):
        raw = []
    reviews: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        reviews.append(
            {
                "interrupt_id": entry.get("interrupt_id"),
                "file_id": entry.get("file_id"),
                "filename": entry.get("filename"),
                "doc_type": entry.get("doc_type"),
                "asked_at": entry.get("asked_at"),
                "reason": entry.get("reason"),
                "options": entry.get("options") or [],
            }
        )
    return json.dumps({"reviews": reviews, "count": len(reviews)}, ensure_ascii=False)


def explain_posted_line(
    tool_context: ToolContext,
    invoice_id: str = "",
    row_index: str = "",
    account_code: str = "",
) -> str:
    """Explain what was posted for an invoice line (ledger + COA + extraction).

    CRITICAL: Do NOT call this tool if the user is asking "why" an account code, COA,
    or tax treatment was used or chosen (e.g., "why did you use this account code?",
    "why this COA?"). For categorization or tax coding explanations, you MUST first
    find the row index by calling `lookup_row` and then call `explain_categorization`
    or `explain_tax_treatment` using that row index.

    This tool is only for audit trail queries and details combining the posted row,
    COA, and extraction logs.

    Args:
        tool_context: Injected by ADK; provides session state.
        invoice_id: Invoice number (e.g. ``25-D15``).
        row_index: Ledger row index from a prior lookup.
        account_code: Optional override when asking about a specific code.

    Returns:
        JSON with ``posted_account_code``, ``coa_description``, ``vendor``,
        ``line_description``, ``extraction_path``, ``categorization_source``,
        and ``review_reasons``.
    """
    from accounting_agents.assistant import THREAD_FOCUS_KEY
    from accounting_agents.assistant_tools._helpers import find_coa_by_code

    try:
        state = tool_context.state
    except Exception:  # noqa: BLE001
        state = {}

    focus = state.get(THREAD_FOCUS_KEY) or {}
    if not isinstance(focus, dict):
        focus = {}

    inv = (invoice_id or focus.get("invoice_id") or "").strip()
    acct_override = (account_code or focus.get("account_code") or "").strip()
    idx: int | None = None
    if row_index:
        try:
            idx = int(str(row_index).strip())
        except (TypeError, ValueError):
            idx = None
    elif focus.get("row_index") is not None:
        try:
            idx = int(focus["row_index"])
        except (TypeError, ValueError):
            idx = None

    rows = _get_rows(tool_context)
    row: dict | None = None
    if idx is not None and 0 <= idx < len(rows):
        row = rows[idx]
    elif inv:
        inv_lower = inv.lower()
        for r in rows:
            inv_num = str(
                r.get("*InvoiceNumber")
                or r.get("Source Filename")
                or r.get("source_filename")
                or ""
            ).lower()
            if inv_lower in inv_num or inv_num.endswith(inv_lower):
                row = r
                break

    if row is None:
        matches = state.get("thread_delivery_ledger_matches") or []
        if isinstance(matches, list):
            for m in matches:
                if not isinstance(m, dict):
                    continue
                if inv and str(m.get("invoice_id") or "").lower() != inv.lower():
                    continue
                return json.dumps(
                    {
                        "status": "partial",
                        "invoice_id": m.get("invoice_id") or inv,
                        "posted_account_code": m.get("account_code") or acct_override,
                        "vendor": m.get("vendor"),
                        "line_description": m.get("description"),
                        "row_index": m.get("row_index"),
                        "source": "thread_delivery_ledger_matches",
                        "coa_description": _coa_description_for_code(
                            state, str(m.get("account_code") or acct_override)
                        ),
                    },
                    ensure_ascii=False,
                )
        return json.dumps(
            {
                "status": "not_found",
                "message": "Need invoice_id or row_index from lookup_row.",
            },
            ensure_ascii=False,
        )

    posted_code = (
        acct_override
        or str(row.get("Account Code / COA") or row.get("*AccountCode") or row.get("category") or "")
    )
    vendor = str(row.get("Vendor") or row.get("*ContactName") or "")
    desc = str(row.get("Description") or row.get("*Description") or "")

    coa_entry = find_coa_by_code(state, posted_code) if posted_code else None
    coa_desc = _coa_description_for_code(state, posted_code)

    extraction_path = ""
    categorization_source = "ledger_row"
    review_reasons: list = []
    file_id = ""
    plog = state.get(PROCESSING_LOG_KEY) or []
    if isinstance(plog, list) and inv:
        for entry in plog:
            if not isinstance(entry, dict):
                continue
            ids = entry.get("invoice_ids") or []
            fn = str(entry.get("filename") or "").lower()
            if inv.lower() in fn or inv in [str(x) for x in ids]:
                file_id = str(entry.get("file_id") or "")
                extraction_path = str(entry.get("extraction_path") or "")
                break

    if file_id:
        detail_raw = get_document_processing_detail(
            tool_context, file_id=file_id,
        )
        try:
            detail = json.loads(detail_raw)
            if isinstance(detail, dict) and detail.get("status") != "not_found":
                extraction_path = str(
                    detail.get("extraction_path") or extraction_path
                )
                review_reasons = detail.get("review_reasons") or []
                categorization_source = "document_session"
        except json.JSONDecodeError:
            pass

    return json.dumps(
        {
            "status": "found",
            "invoice_id": inv or None,
            "posted_account_code": posted_code,
            "coa_description": coa_desc,
            "coa_account_type": (coa_entry or {}).get("account_type"),
            "vendor": vendor,
            "line_description": desc,
            "extraction_path": extraction_path or None,
            "categorization_source": categorization_source,
            "review_reasons": review_reasons,
        },
        ensure_ascii=False,
    )


def _coa_description_for_code(state: dict, account_code: str) -> str:
    from accounting_agents.assistant_tools._helpers import find_coa_by_code

    entry = find_coa_by_code(state, account_code)
    if not entry:
        return ""
    return str(
        entry.get("description") or entry.get("name") or entry.get("key") or ""
    )
