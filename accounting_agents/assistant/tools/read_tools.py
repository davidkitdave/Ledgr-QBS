"""Read-only ledger query tools for the chat assistant."""

from __future__ import annotations

import json
from datetime import date, timedelta

from google.adk.tools import ToolContext

from accounting_agents import config

from ..constants import (
    LEDGER_DATA_KEY,
    PROCESSING_LOG_KEY,
    THREAD_FOCUS_KEY,
)
from ._helpers import (
    _MONTHS,
    _diagnostic_counts,
    _empty_ledger_message,
    _get_rows,
    _is_bank_row,
    _month_year_of,
    _parse_int_param,
    _parse_row_date,
    _tax_registration_threshold,
    _to_float,
    find_coa_by_code,
    filename_matches_query,
    row_search_text,
)


def summarize_by_category(tool_context: ToolContext) -> str:
    """Return total spend (total purchases / expenses) grouped by account / COA category.

    Use this tool whenever the user asks for total purchases, total spend, or expense summaries.
    Do NOT use `pnl_for_fy` for purchases or spend queries unless the user asks for a full Profit & Loss.

    Args:
        tool_context: Injected by ADK; provides access to session state.

    Returns:
        JSON string ``{"totals": {"CategoryName": amount, ...}}`` or a
        human-readable message when the ledger is empty.
    """
    rows = _get_rows(tool_context)
    if not rows:
        return _empty_ledger_message(tool_context)

    totals: dict[str, float] = {}
    for row in rows:
        category = str(row.get("Account Code / COA") or row.get("category") or "Uncategorized")
        try:
            amount = float(row.get("Source Amount") or row.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        totals[category] = totals.get(category, 0.0) + amount

    # Sort descending by spend so the LLM can easily spot the biggest.
    sorted_totals = dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))
    return json.dumps({"totals": sorted_totals}, ensure_ascii=False)


def pnl_for_fy(tool_context: ToolContext) -> str:
    """Return a simple P&L summary (total revenue minus total expenses).

    CRITICAL: Do NOT use this tool if the user only asks for total purchases, total spend,
    or expense summaries. For purchases/spend/expenses, use `summarize_by_category` instead.
    Only use this tool when the user specifically asks for overall profit, net profit,
    total revenue, or a full Profit & Loss summary.

    Args:
        tool_context: Injected by ADK; provides access to session state.

    Returns:
        JSON string ``{"revenue": x, "expenses": y, "net": z}`` or a message
        when the ledger is not loaded.
    """
    rows = _get_rows(tool_context)
    if not rows:
        return _empty_ledger_message(tool_context)

    revenue = 0.0
    expenses = 0.0
    for row in rows:
        try:
            amount = float(row.get("Source Amount") or row.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0

        doc_type = str(row.get("Doc Type") or "").strip().upper()
        if doc_type in ("S", "SALES"):
            revenue += amount
        elif doc_type in ("P", "PURCHASE"):
            expenses += amount
        else:
            # Fallback: positive = revenue, negative = expense.
            if amount >= 0:
                revenue += amount
            else:
                expenses += abs(amount)

    net = revenue - expenses
    return json.dumps(
        {"revenue": round(revenue, 2), "expenses": round(expenses, 2), "net": round(net, 2)},
        ensure_ascii=False,
    )


def bank_totals(tool_context: ToolContext, month: str = "", year: str = "") -> str:
    """Totals for the client's bank statement: withdrawals, deposits, net, balances.

    Operates on bank-statement rows (``Withdrawal`` / ``Deposit`` / ``Balance``
    columns), so use THIS tool — not the invoice tools — for any bank-statement
    question (e.g. "total withdrawals in October", "closing balance", "how much
    came in").  Optionally filter to one month.

    Args:
        tool_context: Injected by ADK; provides session state access.
        month: Optional month filter — name, abbreviation, or number
            (e.g. "October", "Oct", "10"). Empty = all months.
        year: Optional 4-digit year filter (e.g. "2025"). Empty = any year.

    Returns:
        JSON string with ``withdrawals``, ``deposits``, ``net`` (deposits −
        withdrawals), ``transaction_count``, ``opening_balance``,
        ``closing_balance``, ``currency``, and ``period``; or a human-readable
        message when no bank data is loaded / the month has no rows.
    """
    rows = [r for r in _get_rows(tool_context) if _is_bank_row(r)]
    if not rows:
        return (
            "No bank-statement data is loaded for this client. Upload the bank "
            "statement(s) first, or ask about the invoice ledger instead."
        )

    # Resolve the optional month filter.
    want_month: int | None = None
    m = (month or "").strip().lower()
    if m:
        want_month = _MONTHS.get(m)
        if want_month is None and m.isdigit():
            want_month = int(m)
    want_year: int | None = int(year) if (year or "").strip().isdigit() else None

    filtering = want_month is not None or want_year is not None
    withdrawals = deposits = 0.0
    txn_count = 0
    opening_balance: float | None = None
    closing_balance: float | None = None
    currency = "SGD"
    # Running balance seen so far (B/F or any prior row), so a filtered period's
    # opening balance is the balance immediately BEFORE its first transaction —
    # not the first B/F in the whole sheet.
    prev_balance: float | None = None

    def _bal(r):
        b = r.get("Balance")
        return _to_float(b) if b is not None and str(b).strip() != "" else None

    for row in rows:
        desc = str(row.get("Description") or "").strip().upper()
        if row.get("Currency"):
            currency = row["Currency"]

        # BALANCE B/F marks a block opening; never summed.
        if desc == "BALANCE B/F":
            bf = _bal(row)
            prev_balance = bf if bf is not None else prev_balance
            if not filtering and opening_balance is None:
                opening_balance = bf
            continue
        if desc == "TOTALS":
            continue

        in_period = True
        if filtering:
            mth, yr = _month_year_of(row.get("Date"))
            if want_month is not None and mth != want_month:
                in_period = False
            if want_year is not None and yr != want_year:
                in_period = False
        if not in_period:
            # Advance the running balance so the next in-period opening is correct.
            b = _bal(row)
            if b is not None:
                prev_balance = b
            continue

        # In-period transaction. For a filtered query, the opening balance is the
        # balance just before the first matching row.
        if filtering and opening_balance is None:
            opening_balance = prev_balance

        withdrawals += _to_float(row.get("Withdrawal"))
        deposits += _to_float(row.get("Deposit"))
        txn_count += 1
        b = _bal(row)
        if b is not None:
            closing_balance = b
            prev_balance = b

    if txn_count == 0 and (want_month is not None or want_year is not None):
        return json.dumps(
            {"transaction_count": 0, "period": f"{month} {year}".strip(),
             "message": "No transactions found for that period."},
            ensure_ascii=False,
        )

    period = " ".join(p for p in (month, year) if p).strip() or "all loaded months"
    return json.dumps(
        {
            "withdrawals": round(withdrawals, 2),
            "deposits": round(deposits, 2),
            "net": round(deposits - withdrawals, 2),
            "transaction_count": txn_count,
            "opening_balance": round(opening_balance, 2) if opening_balance is not None else None,
            "closing_balance": round(closing_balance, 2) if closing_balance is not None else None,
            "currency": currency,
            "period": period,
        },
        ensure_ascii=False,
    )


def gst_threshold_check(tool_context: ToolContext) -> str:
    """Check whether taxable turnover is approaching the jurisdiction's registration threshold.

    Sums ``Source Amount`` for rows where ``Tax Rate`` indicates a standard-
    rated or zero-rated supply (SR / ZR / SSR — covers both Singapore GST
    and Malaysia SST). Compares against the active jurisdiction's mandatory
    registration threshold (SG: SGD 1M, MY: MYR 500K — read from
    :func:`_tax_registration_threshold`).

    Per ADK best practice: region is read from ``state["region"]`` /
    ``state["client_region"]`` via :func:`_tax_registration_threshold`. The
    tool returns the threshold currency + label so the chat agent can
    surface the correct number to the user (no more "SGD 1 M" answer for a
    Malaysia client).

    Args:
        tool_context: Injected by ADK; provides access to session state.

    Returns:
        JSON string with ``taxable_turnover``, ``threshold``, ``currency``,
        ``threshold_label``, ``headroom``, and ``near_threshold`` (bool, True
        when within 20 % of the limit).
    """
    rows = _get_rows(tool_context)
    if not rows:
        return _empty_ledger_message(tool_context)

    threshold, currency, label = _tax_registration_threshold(
        getattr(tool_context, "state", {}) or {}
    )
    if not threshold or not currency:
        return json.dumps(
            {
                "status": "unknown_region",
                "message": (
                    "Client tax region is not set or unsupported; "
                    "cannot compare turnover to a registration threshold."
                ),
            },
            ensure_ascii=False,
        )

    taxable = 0.0
    for row in rows:
        tax_rate = str(row.get("Tax Rate") or row.get("tax_rate") or "").strip().upper()
        # Standard-rated (9% SR) and zero-rated (ZR) supplies count toward
        # the taxable turnover threshold for both SG (GST) and MY (SST).
        # SSR is Malaysia Sales Tax — also counts. Exempt (ES/EP) and
        # out-of-scope (OS) do not.
        if tax_rate in ("SR", "ZR", "SR9", "SR8", "SR7", "SSR"):
            try:
                amount = float(row.get("Source Amount") or row.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            taxable += abs(amount)

    headroom = threshold - taxable
    near = taxable >= threshold * 0.80
    return json.dumps(
        {
            "taxable_turnover": round(taxable, 2),
            "threshold": threshold,
            "currency": currency,
            "threshold_label": label,
            "headroom": round(max(headroom, 0.0), 2),
            "near_threshold": near,
            "already_exceeded": taxable >= threshold,
        },
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------- #
# Read-only inspection tools (Step 1.2)
# --------------------------------------------------------------------------- #


def show_client_profile(tool_context: ToolContext) -> str:
    """Return the loaded client profile + learned-mapping sizes as JSON.

    Pulls profile keys from session state (set by the Slack runner from
    ``ClientContext.to_state()``) and the counts of ``coa`` + ``entity_memory``
    so the user can see how much context the assistant has loaded.

    Returns:
        JSON string with ``client_name``, ``client_uen``, ``region``,
        ``base_currency``, ``tax_registered``, ``fye_month``, ``coa_count``,
        and ``entity_memory_count`` — or a friendly message when no profile
        is loaded.
    """
    try:
        state = tool_context.state
        client_name = state.get("client_name")
    except Exception:  # noqa: BLE001 — never let a tool crash the lane
        return "No client profile is loaded yet for this channel."

    if not client_name:
        return "No client profile is loaded yet for this channel."

    software = state.get("software")
    onboarding_required = bool(state.get("onboarding_required")) or not software
    coa = state.get("coa")
    entity_memory = state.get("entity_memory")
    coa_count = len(coa) if isinstance(coa, list) else 0
    entity_memory_count = len(entity_memory) if isinstance(entity_memory, list) else 0
    return json.dumps(
        {
            "client_name": client_name,
            "client_uen": state.get("client_uen"),
            "region": state.get("region"),
            "base_currency": state.get("base_currency"),
            "tax_registered": state.get("tax_registered"),
            "fye_month": state.get("fye_month"),
            "software": software,
            "onboarding_required": onboarding_required,
            "coa_count": coa_count,
            "entity_memory_count": entity_memory_count,
        },
        ensure_ascii=False,
    )


def show_learned_mappings(tool_context: ToolContext) -> str:
    """Return the per-client learned category/entity mappings as JSON.

    Reads ``state["category_mapping"]`` (a vendor/keyword → COA map) and
    ``state["entity_memory"]`` (remembered entities) populated by the
    pipeline's learning loop.

    Returns:
        JSON string ``{"category_mapping": {...}, "entity_memory": [...]}`` —
        or a friendly message when both are empty / absent.
    """
    try:
        state = tool_context.state
        category_mapping = state.get("category_mapping")
        entity_memory = state.get("entity_memory")
    except Exception:  # noqa: BLE001
        return "No learned mappings yet — process some documents first."

    has_cat = isinstance(category_mapping, dict) and category_mapping
    has_ent = isinstance(entity_memory, list) and entity_memory
    if not has_cat and not has_ent:
        return "No learned mappings yet — process some documents first."

    return json.dumps(
        {
            "category_mapping": category_mapping if has_cat else {},
            "entity_memory": entity_memory if has_ent else [],
        },
        ensure_ascii=False,
    )


def model_info(tool_context: ToolContext) -> str:  # noqa: ARG001 — uniform tool signature
    """Return which Gemini models back this assistant + the document pipeline.

    Returns:
        JSON string with ``chat_model`` (this assistant's model), ``model_lite``
        (invoice/chat tier), and ``model_std`` (bank/complex tier).
    """
    return json.dumps(
        {
            "chat_model": config.MODEL_CHAT,
            "model_lite": config.MODEL_LITE,
            "model_std": config.MODEL_STD,
            "model_chat": config.MODEL_CHAT,
        },
        ensure_ascii=False,
    )


def lookup_coa_account(tool_context: ToolContext, account_code: str) -> str:
    """Return COA description, type, and keywords for a posted account code.

    Reads the client's chart of accounts from session state (``coa`` list
    injected by the runner from ``ClientContext``). Use when the user asks
    what a code *means* in their COA — distinct from ``explain_categorization``,
    which re-runs the engine's pick logic for a vendor/line, and from
    ``explain_posted_line``, which combines ledger + extraction audit detail.

    Args:
        tool_context: Injected by ADK; provides session state.
        account_code: The COA code to look up (e.g. ``902-A02``, ``6-3000``).

    Returns:
        JSON with ``status`` ``found`` or ``not_found`` and COA fields when found.
    """
    try:
        state = tool_context.state
    except Exception:  # noqa: BLE001
        state = {}

    code = (account_code or "").strip()
    if not code:
        focus = state.get(THREAD_FOCUS_KEY) or {}
        if isinstance(focus, dict):
            code = str(focus.get("account_code") or "").strip()
    if not code:
        return json.dumps(
            {
                "status": "error",
                "message": "Need account_code (or thread_focus from a prior turn).",
            },
            ensure_ascii=False,
        )

    entry = find_coa_by_code(state, code)
    if not entry:
        return json.dumps(
            {
                "status": "not_found",
                "account_code": code,
                "message": f"No COA entry for code {code!r} in the loaded chart.",
            },
            ensure_ascii=False,
        )

    description = (
        entry.get("description")
        or entry.get("name")
        or entry.get("key")
        or ""
    )
    return json.dumps(
        {
            "status": "found",
            "code": entry.get("code") or code,
            "description": description,
            "account_type": entry.get("account_type"),
            "financial_statement": entry.get("financial_statement"),
            "nature": entry.get("nature"),
            "keywords": entry.get("keywords"),
        },
        ensure_ascii=False,
    )
def summarize_recent_activity(tool_context: ToolContext, days: str = "30") -> str:
    """Summarise ledger activity in the last N days.

    Filters ``state["ledger_data"]`` to rows whose ``Date`` falls within the
    window (default 30 days). Skips bank-statement rows.

    Args:
        tool_context: Injected by ADK; provides session state.
        days: Look-back window in days (default ``30``).

    Returns:
        JSON with ``period_days``, ``transaction_count``, ``total_spend``,
        ``by_category``, ``by_doc_type``, and ``flagged_count``.
    """
    rows = _get_rows(tool_context)
    if not rows:
        return _empty_ledger_message(tool_context)

    window = _parse_int_param(days, default=30, minimum=1, maximum=366)
    cutoff = date.today() - timedelta(days=window)
    by_category: dict[str, float] = {}
    by_doc_type: dict[str, float] = {"S": 0.0, "P": 0.0}
    total_spend = 0.0
    txn_count = 0
    flagged_count = 0

    for row in rows:
        if _is_bank_row(row):
            continue
        row_date = _parse_row_date(row.get("Date"))
        if row_date is None or row_date < cutoff:
            continue

        amount = _to_float(row.get("Source Amount") or row.get("amount"))
        category = str(row.get("Account Code / COA") or row.get("category") or "Uncategorized")
        by_category[category] = by_category.get(category, 0.0) + amount
        total_spend += amount
        txn_count += 1

        doc_type = str(row.get("Doc Type") or "").strip().upper()
        if doc_type in by_doc_type:
            by_doc_type[doc_type] += amount

        if row.get("Review") or row.get("Flagged"):
            flagged_count += 1

    if txn_count == 0:
        # Find the most recent date across ALL rows (invoice AND bank) so the
        # user knows what period IS available and can ask a smarter follow-up.
        newest: date | None = None
        for row in rows:
            rd = _parse_row_date(row.get("Date"))
            if rd is not None and (newest is None or rd > newest):
                newest = rd
        newest_hint = f" The newest entry I see is from {newest.isoformat()}." if newest else ""
        return (
            f"No transactions found in the last {window} days.{newest_hint}"
            f" Ask me for that month or the full FY if you'd like a wider view."
        )

    return json.dumps(
        {
            "period_days": window,
            "transaction_count": txn_count,
            "total_spend": round(total_spend, 2),
            "by_category": dict(
                sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)
            ),
            "by_doc_type": {k: round(v, 2) for k, v in by_doc_type.items()},
            "flagged_count": flagged_count,
        },
        ensure_ascii=False,
    )


def lookup_row(tool_context: ToolContext, query: str, limit: str = "5") -> str:
    """Search loaded ledger rows by substring (case-insensitive).

    Matches Description, Vendor, Reference, Source Filename, invoice number
    (``*InvoiceNumber`` / ``Xero:…``), and contact name. When nothing matches
    in the loaded ledger, also searches the processing log so partial filenames
    like ``25-D12`` still resolve to a delivery (and its FY).

    Use this BEFORE ``explain_categorization`` or ``explain_tax_treatment`` when
    the user asks *why* a line was coded a certain way — pass the returned
    ``row_index`` into those explain tools. For audit-trail / posted-line detail
    (not "why" questions), use ``explain_posted_line`` instead.

    Args:
        tool_context: Injected by ADK; provides session state.
        query: Substring to search for (invoice id, filename fragment, vendor).
        limit: Maximum matches to return (default ``5``, max ``20``).

    Returns:
        JSON with ``matches`` (ledger hits) and optional ``processing_log_matches``.
    """

    rows = _get_rows(tool_context)
    needle = (query or "").strip().lower()
    if not needle:
        return json.dumps({"matches": [], "processing_log_matches": []}, ensure_ascii=False)

    if not rows:
        plog_hits = _processing_log_hits(tool_context, needle)
        payload: dict = {
            "status": "empty_ledger",
            "message": _empty_ledger_message(tool_context),
            "matches": [],
        }
        if plog_hits:
            payload["processing_log_matches"] = plog_hits
        return json.dumps(payload, ensure_ascii=False)

    cap = _parse_int_param(limit, default=5, minimum=1, maximum=20)
    matches: list[dict] = []

    for idx, row in enumerate(rows):
        if needle not in row_search_text(row):
            continue
        matches.append(
            {
                "row_index": idx,
                "sheet": row.get("_sheet"),
                "account_code": row.get("Account Code / COA") or row.get("category"),
                "amount": _to_float(row.get("Source Amount") or row.get("amount")),
                "date": row.get("Date"),
                "description": row.get("Description"),
                "vendor": row.get("Vendor"),
                "tax_rate": row.get("Tax Rate") or row.get("tax_rate"),
                "doc_type": row.get("Doc Type"),
            }
        )
        if len(matches) >= cap:
            break

    payload: dict = {"matches": matches}
    if not matches:
        plog_hits = _processing_log_hits(tool_context, needle)
        if plog_hits:
            payload["processing_log_matches"] = plog_hits
            diag = _diagnostic_counts(tool_context)
            loaded = str(diag.get("fy_loaded") or "")
            hit_fys = {str(h.get("fy") or "") for h in plog_hits}
            if loaded and hit_fys - {loaded}:
                payload["hint"] = (
                    f"Found {len(plog_hits)} processing-log hit(s) in FY "
                    f"{', '.join(sorted(hit_fys))} but the loaded ledger is "
                    f"FY{loaded}. Re-ask after the session loads that FY, or "
                    "call diagnose_assistant_context."
                )

    return json.dumps(payload, ensure_ascii=False)


def _processing_log_hits(tool_context: ToolContext, needle: str) -> list[dict]:
    raw_log = tool_context.state.get(PROCESSING_LOG_KEY) or []
    if not isinstance(raw_log, list):
        return []
    hits: list[dict] = []
    for entry in raw_log:
        if not isinstance(entry, dict):
            continue
        fn = str(entry.get("filename") or "")
        if filename_matches_query(needle, fn):
            hits.append(
                {
                    "filename": entry.get("filename"),
                    "file_id": entry.get("file_id"),
                    "fy": entry.get("fy"),
                    "doc_type": entry.get("doc_type"),
                    "row_count": entry.get("row_count"),
                }
            )
    return hits


def list_recent_documents(tool_context: ToolContext, limit: str = "10") -> str:
    """List source documents grouped from the loaded FY ledger rows.

    Groups by ``(Source Filename, Doc Type / sheet)``. Covers both invoice rows
    (Purchase / Sales) and bank-statement rows (Withdrawal / Deposit / Balance)
    so a channel that only has a bank statement doesn't return an empty list.

    Use this to browse source documents by filename/vendor/date in the loaded
    ledger. For chronological delivery history (what the bot processed), use
    ``list_processing_history``. For per-file extraction deep-dives, use
    ``get_document_processing_detail``.

    For invoice rows the representative date is the ``Date`` column value.
    For bank rows the representative date is the earliest transaction date in the
    group (the statement opening date), and ``doc_type`` is ``"Bank"``.

    Args:
        tool_context: Injected by ADK; provides session state.
        limit: Maximum documents to return (default ``10``, max ``50``).

    Returns:
        JSON ``{"documents": [{date, filename, doc_type, row_count, total, ...}]}``.
    """
    rows = _get_rows(tool_context)
    if not rows:
        return _empty_ledger_message(tool_context)

    cap = _parse_int_param(limit, default=10, minimum=1, maximum=50)
    groups: dict[tuple, dict] = {}

    for row in rows:
        is_bank = _is_bank_row(row)
        filename = str(row.get("Source Filename") or row.get("source_filename") or "unknown")
        if is_bank:
            # Group bank rows by filename + sheet (one entry per uploaded statement).
            doc_type = "Bank"
            sheet = str(row.get("_sheet") or "Bank")
            key = (filename, doc_type, sheet)
        else:
            doc_type = str(row.get("Doc Type") or "")
            key = (
                filename,
                doc_type,
                str(row.get("Date") or ""),
            )

        if key not in groups:
            groups[key] = {
                "date": str(row.get("Date") or ""),
                "filename": filename,
                "doc_type": doc_type,
                "row_count": 0,
                "total": 0.0,
                "currency": row.get("Currency") or row.get("currency") or "SGD",
                "flagged_count": 0,
            }
        entry = groups[key]
        entry["row_count"] += 1

        if is_bank:
            # Use the earliest date in the group as the representative date so
            # the document sorts near its statement month, not the last row.
            row_date_str = str(row.get("Date") or "")
            if row_date_str and (
                not entry["date"]
                or (_parse_row_date(row_date_str) or date.max)
                < (_parse_row_date(entry["date"]) or date.max)
            ):
                entry["date"] = row_date_str
            # Net figure: deposits − withdrawals (positive = net inflow).
            entry["total"] += _to_float(row.get("Deposit")) - _to_float(row.get("Withdrawal"))
        else:
            entry["total"] += _to_float(row.get("Source Amount") or row.get("amount"))

        if row.get("Review") or row.get("Flagged"):
            entry["flagged_count"] += 1

    documents = sorted(
        groups.values(),
        key=lambda d: (_parse_row_date(d["date"]) or date.min, d["filename"]),
        reverse=True,
    )[:cap]

    log_by_filename: dict[str, dict] = {}
    log_by_file_id: dict[str, dict] = {}
    try:
        raw_log = tool_context.state.get(PROCESSING_LOG_KEY) or []
        if isinstance(raw_log, list):
            for entry in raw_log:
                if not isinstance(entry, dict):
                    continue
                fn = str(entry.get("filename") or "").strip().lower()
                fid = str(entry.get("file_id") or "").strip()
                if fn:
                    log_by_filename[fn] = entry
                if fid:
                    log_by_file_id[fid] = entry
    except Exception:  # noqa: BLE001
        pass

    for doc in documents:
        doc["total"] = round(doc["total"], 2)
        meta = log_by_filename.get(str(doc.get("filename") or "").strip().lower())
        if meta:
            doc["extraction_path"] = meta.get("extraction_path")
            doc["pipeline_doc_type"] = meta.get("doc_type")
            doc["file_id"] = meta.get("file_id")

    return json.dumps({"documents": documents}, ensure_ascii=False)
