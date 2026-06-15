"""Read-only accounting chat LlmAgent over the client's FY ledger.

The agent answers questions about the client's books using pure, deterministic
function tools that operate on ``state["ledger_data"]`` — a list of row dicts
injected by the Slack runner before each turn.  NO Slack or network calls
happen inside the tools; the runner layer owns data fetching.

This agent is a **standalone root agent** (run on its own chat Runner, see
``accounting_agents.agent.assistant_app``), NOT a graph node — so it carries no
``mode`` setting and sees the full per-thread session history (multi-turn).
See ``docs/adr/0008-chat-lane-standalone-root-agent.md``.

State contract
--------------
``state["ledger_data"]`` : list[dict]
    Each dict is one ledger row with string keys matching the workbook column
    headers (e.g. "Account Code / COA", "Source Amount", "Date", "Doc Type",
    "Tax Rate", ...).  The runner injects this before each turn.
    If the key is absent or the list is empty the agent tells the user the
    ledger is not loaded yet rather than hallucinating.

The runner also injects the client profile keys (``client_name``,
``client_uen``, ``region``, ``base_currency``, ``tax_registered``,
``fye_month``) so ``assistant_instruction`` can tell the agent who the
client is, plus ``category_mapping`` / ``entity_memory`` learned-mapping
state surfaced by the inspection tools.

Tools
-----
- ``bank_totals``             — withdrawals/deposits/balances for bank rows
- ``summarize_by_category``   — total spend per COA / category
- ``pnl_for_fy``              — revenue minus expenses over all rows
- ``gst_threshold_check``     — compare total taxable turnover to SGD 1 M
- ``show_client_profile``     — the loaded client profile (read-only)
- ``show_learned_mappings``   — learned category/entity mappings (read-only)
- ``model_info``              — which Gemini models back this assistant
- ``explain_categorization``  — why a line maps to a COA account (engine logic)
- ``explain_tax_treatment``   — why a line gets a tax code (engine logic)
- ``summarize_recent_activity`` — spend/activity in the last N days
- ``lookup_row``              — search ledger rows by text
- ``list_recent_documents``   — grouped list of source documents in the FY ledger

All tools are pure (no I/O, no randomness) so they are trivially testable.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext

from invoice_processing.export.categorizer import resolve_account
from invoice_processing.export.client_context import (
    category_mapping_from_state,
    coa_from_state,
    entity_memory_from_state,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from invoice_processing.export.tax_classifier import TaxClassifier

from . import config

# --------------------------------------------------------------------------- #
# Pure ledger tools (operate on rows already in session state)
# --------------------------------------------------------------------------- #

#: The session state key the runner must set before routing to the chat path.
LEDGER_DATA_KEY = "ledger_data"

#: SGD threshold for mandatory GST registration (s.40B GST Act, Singapore).
GST_THRESHOLD_SGD = 1_000_000.0


def _get_rows(tool_context: ToolContext) -> list[dict]:
    """Return the ledger rows from session state (empty list if absent)."""
    rows = tool_context.state.get(LEDGER_DATA_KEY)
    if not isinstance(rows, list):
        return []
    return rows


def summarize_by_category(tool_context: ToolContext) -> str:
    """Return total spend grouped by account / COA category.

    Reads ``state["ledger_data"]`` and sums ``Source Amount`` per
    ``Account Code / COA`` value.  Returns a JSON string so the LLM can
    render it cleanly.

    Args:
        tool_context: Injected by ADK; provides access to session state.

    Returns:
        JSON string ``{"totals": {"CategoryName": amount, ...}}`` or a
        human-readable message when the ledger is empty.
    """
    rows = _get_rows(tool_context)
    if not rows:
        return "The ledger data is not loaded yet. Please upload the FY ledger first."

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

    Uses the ``Doc Type`` field to separate sales (``S``) from purchases
    (``P``).  Falls back to the sign of ``Source Amount`` (positive = revenue,
    negative = expense) when ``Doc Type`` is absent.

    Args:
        tool_context: Injected by ADK; provides access to session state.

    Returns:
        JSON string ``{"revenue": x, "expenses": y, "net": z}`` or a message
        when the ledger is not loaded.
    """
    rows = _get_rows(tool_context)
    if not rows:
        return "The ledger data is not loaded yet. Please upload the FY ledger first."

    revenue = 0.0
    expenses = 0.0
    for row in rows:
        try:
            amount = float(row.get("Source Amount") or row.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0

        doc_type = str(row.get("Doc Type") or "").strip().upper()
        if doc_type == "S":
            revenue += amount
        elif doc_type == "P":
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


#: Month-name / abbreviation → month number, for bank period filtering.
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _is_bank_row(row: dict) -> bool:
    """True when a row looks like a bank-statement line (has bank columns)."""
    return any(k in row for k in ("Withdrawal", "Deposit", "Balance"))


def _month_year_of(value) -> tuple[int, int] | tuple[None, None]:
    """Extract (month, year) from a bank Date cell (``DD/MM/YYYY`` str or date)."""
    if value is None:
        return (None, None)
    # date / datetime object.
    month = getattr(value, "month", None)
    year = getattr(value, "year", None)
    if month and year:
        return (int(month), int(year))
    # String "DD/MM/YYYY" (or "DD/MM/YY").
    parts = str(value).strip().split("/")
    if len(parts) == 3:
        try:
            mth = int(parts[1])
            yr = int(parts[2])
            if yr < 100:
                yr += 2000
            return (mth, yr)
        except ValueError:
            return (None, None)
    return (None, None)


def _to_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_row_date(value) -> date | None:
    """Parse a ledger ``Date`` cell (``DD/MM/YYYY`` str or date object)."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    parts = str(value).strip().split("/")
    if len(parts) == 3:
        try:
            day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
            if year < 100:
                year += 2000
            return date(year, month, day)
        except ValueError:
            return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _parse_int_param(value: str, default: int, *, minimum: int = 1, maximum: int) -> int:
    """Parse an ADK string tool param to a bounded int."""
    try:
        n = int(str(value or "").strip() or default)
    except ValueError:
        n = default
    return max(minimum, min(maximum, n))


def _parse_bool_param(value: str, *, default: bool | None = None) -> bool | None:
    """Parse yes/no/true/false tool param; empty string → ``default``."""
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    if raw in ("true", "yes", "1"):
        return True
    if raw in ("false", "no", "0"):
        return False
    return default


def _categorization_reason(source: str, res) -> str:
    """Human-readable reason string for a categorization resolution branch."""
    if source == "entity_memory":
        return (
            f"Vendor matched a remembered entity_memory entry "
            f"(confidence {res.confidence})."
        )
    if source == "category_mapping":
        return (
            f"Universal category mapped to account {res.account_code} "
            f"(confidence {res.confidence})."
        )
    if source == "coa_keyword":
        return (
            f"Line or vendor text matched a COA keyword for "
            f"{res.account_name or res.account_code} (confidence {res.confidence})."
        )
    return "No deterministic match — line would be flagged for review or LLM fallback."


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
    """Check whether taxable turnover is approaching the SGD 1 M GST threshold.

    Sums ``Source Amount`` for rows where ``Tax Rate`` indicates a standard-
    rated supply (SR / ZR for goods; ignores exempt / out-of-scope).  Compares
    against the ``SGD 1,000,000`` mandatory registration threshold.

    Args:
        tool_context: Injected by ADK; provides access to session state.

    Returns:
        JSON string with ``taxable_turnover``, ``threshold``, ``headroom``,
        and ``near_threshold`` (bool, True when within 20 % of the limit).
    """
    rows = _get_rows(tool_context)
    if not rows:
        return "The ledger data is not loaded yet. Please upload the FY ledger first."

    taxable = 0.0
    for row in rows:
        tax_rate = str(row.get("Tax Rate") or row.get("tax_rate") or "").strip().upper()
        # Standard-rated (9% SR) and zero-rated (ZR) supplies count toward
        # the taxable turnover threshold; exempt (ES/EP) and out-of-scope (OS)
        # do not.
        if tax_rate in ("SR", "ZR", "SR9", "SR8", "SR7"):
            try:
                amount = float(row.get("Source Amount") or row.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            taxable += abs(amount)

    headroom = GST_THRESHOLD_SGD - taxable
    near = taxable >= GST_THRESHOLD_SGD * 0.80
    return json.dumps(
        {
            "taxable_turnover": round(taxable, 2),
            "threshold": GST_THRESHOLD_SGD,
            "headroom": round(max(headroom, 0.0), 2),
            "near_threshold": near,
            "already_exceeded": taxable >= GST_THRESHOLD_SGD,
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
            "chat_model": config.MODEL_LITE,
            "model_lite": config.MODEL_LITE,
            "model_std": config.MODEL_STD,
        },
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------- #
# Explain + lookup read tools (Step 3 / C-1)
# --------------------------------------------------------------------------- #


def explain_categorization(
    tool_context: ToolContext,
    vendor_name: str,
    line_description: str,
    category: str = "",
) -> str:
    """Explain why a line would map to a COA account using the engine's categorizer.

    Re-runs the same deterministic ``resolve_account`` logic the document pipeline
    uses (entity_memory → category_mapping → COA keyword). Does NOT call the LLM
    fallback — this explains the first-pass deterministic path only.

    Args:
        tool_context: Injected by ADK; provides session state.
        vendor_name: Supplier / vendor name on the invoice line.
        line_description: The line item description.
        category: Optional universal category label (for category_mapping lookups).

    Returns:
        JSON with ``status``, ``account_code``, ``account_name``, ``confidence``,
        ``source``, ``flagged``, and ``reason``.
    """
    try:
        state = tool_context.state
    except Exception:  # noqa: BLE001
        state = {}

    res = resolve_account(
        line_description,
        vendor_name,
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
    """Explain why a line gets a tax treatment code using the engine's tax classifier.

    Builds a one-line ``NormalizedInvoice`` in memory and runs
    ``TaxClassifier.classify_line`` — the same logic as ``tax_node``. Honours the
    §0.5-C master gate: when the client is not GST-registered, every line is ``NT``.

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
        ``tax_reason``.
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
        supplier=PartyInfo(name="Supplier", country="SG"),
        customer=PartyInfo(name="Customer", country="SG"),
        our_gst_registered=reg,
    )
    TaxClassifier().classify_line(line, inv)
    return json.dumps(
        {
            "tax_treatment": line.tax_treatment,
            "tax_confidence": line.tax_confidence,
            "tax_flagged": line.tax_flagged,
            "tax_reason": line.tax_reason,
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
        return "The ledger data is not loaded yet. Please upload the FY ledger first."

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

    Matches against ``Description``, ``Vendor``, ``Reference``, and
    ``Account Code / COA`` columns.

    Args:
        tool_context: Injected by ADK; provides session state.
        query: Substring to search for.
        limit: Maximum matches to return (default ``5``, max ``20``).

    Returns:
        JSON ``{"matches": [...]}`` — empty list when nothing matches.
    """
    rows = _get_rows(tool_context)
    if not rows:
        return "The ledger data is not loaded yet. Please upload the FY ledger first."

    needle = (query or "").strip().lower()
    if not needle:
        return json.dumps({"matches": []}, ensure_ascii=False)

    cap = _parse_int_param(limit, default=5, minimum=1, maximum=20)
    matches: list[dict] = []
    for idx, row in enumerate(rows):
        haystack = " ".join(
            str(row.get(col) or "")
            for col in ("Description", "Vendor", "Reference", "Account Code / COA")
        ).lower()
        if needle not in haystack:
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

    return json.dumps({"matches": matches}, ensure_ascii=False)


def list_recent_documents(tool_context: ToolContext, limit: str = "10") -> str:
    """List source documents grouped from the loaded FY ledger rows.

    Groups by ``(Date, Source Filename, Doc Type)``. Only covers documents
    present in the currently loaded workbook — not a cross-FY job log.

    Args:
        tool_context: Injected by ADK; provides session state.
        limit: Maximum documents to return (default ``10``, max ``50``).

    Returns:
        JSON ``{"documents": [{date, filename, doc_type, row_count, total, ...}]}``.
    """
    rows = _get_rows(tool_context)
    if not rows:
        return "The ledger data is not loaded yet. Please upload the FY ledger first."

    cap = _parse_int_param(limit, default=10, minimum=1, maximum=50)
    groups: dict[tuple, dict] = {}

    for row in rows:
        if _is_bank_row(row):
            continue
        key = (
            str(row.get("Date") or ""),
            str(row.get("Source Filename") or row.get("source_filename") or "unknown"),
            str(row.get("Doc Type") or ""),
        )
        if key not in groups:
            groups[key] = {
                "date": key[0],
                "filename": key[1],
                "doc_type": key[2],
                "row_count": 0,
                "total": 0.0,
                "currency": row.get("Currency") or row.get("currency") or "SGD",
                "flagged_count": 0,
            }
        entry = groups[key]
        entry["row_count"] += 1
        entry["total"] += _to_float(row.get("Source Amount") or row.get("amount"))
        if row.get("Review") or row.get("Flagged"):
            entry["flagged_count"] += 1

    documents = sorted(
        groups.values(),
        key=lambda d: (_parse_row_date(d["date"]) or date.min, d["filename"]),
        reverse=True,
    )[:cap]
    for doc in documents:
        doc["total"] = round(doc["total"], 2)

    return json.dumps({"documents": documents}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Assistant LlmAgent (standalone root — multi-turn, sees session history)
# --------------------------------------------------------------------------- #

_BASE_INSTRUCTION = """
You are a read-only accounting assistant for a Singapore SME's financial ledger.
You answer questions strictly based on the ledger data that has been loaded into
your session — you do NOT make up numbers, guess, or call external services.

The loaded data may be an INVOICE ledger (columns like "Source Amount",
"Account Code / COA", "Doc Type") or a BANK STATEMENT (columns "Withdrawal",
"Deposit", "Balance"). Pick the tool that matches the question:

Bank-statement questions (withdrawals, deposits, money in/out, closing or
opening balance, a given month's totals):
- ``bank_totals``: withdrawals, deposits, net, opening/closing balance — with an
  optional month + year filter (e.g. month="October", year="2025").

Invoice-ledger questions (spend by category, P&L, GST):
- ``summarize_by_category``: total spend per GL account / COA category.
- ``pnl_for_fy``: revenue, expenses, and net profit/loss for the FY.
- ``gst_threshold_check``: whether the business is near the SGD 1 M GST
  registration threshold.

Inspection tools (when the user asks about setup / context / models):
- ``show_client_profile``: the loaded client profile and counts.
- ``show_learned_mappings``: learned category / entity mappings.
- ``model_info``: which Gemini models back this assistant.

Explain + lookup tools (when the user asks *why* or *where*):
- ``explain_categorization``: why a vendor/line maps to a COA account (same engine logic).
- ``explain_tax_treatment``: why a line gets SR/ZR/NT/etc (same tax classifier).
- ``summarize_recent_activity``: spend and activity in the last N days (default 30).
- ``lookup_row``: find ledger rows matching a text query (vendor, description, account).
- ``list_recent_documents``: list source documents grouped from the loaded FY ledger.

For every question, call the single most relevant tool first, then explain the
result in plain English. For a bank question naming a month, pass that month
(and year if given) to ``bank_totals``. If a tool reports that the data is not
loaded, tell the user to upload the relevant workbook first.

CRITICAL — ALWAYS finish your turn with a short plain-English message to the
user. After a tool returns, you MUST write one or two sentences summarising the
answer in your own words, citing the relevant numbers from the tool result.
NEVER end your turn with only a tool call and no text reply — the user cannot
see the raw tool output, so silence looks like a broken assistant.

Be concise and professional. Do not invent figures not returned by the tools.
""".strip()


def assistant_instruction(ctx) -> str:
    """Build the assistant's system prompt with a one-line profile preamble.

    Because the assistant is a standalone root agent (see ADR-0008), it sees
    the user turn + full session history via ``include_contents='default'``,
    so we no longer embed the question into the prompt. Instead we prepend a
    short profile preamble — pulled defensively from the per-channel state
    keys produced by ``ClientContext.to_state()`` — so the model knows who
    the client is on every turn. Falls back to the base instruction when no
    profile is loaded so prompt assembly never crashes the lane.
    """
    try:
        state = ctx.state
        client_name = (state.get("client_name") or "").strip()
    except Exception:  # noqa: BLE001 — never let prompt assembly crash the lane
        return _BASE_INSTRUCTION

    if not client_name:
        return _BASE_INSTRUCTION

    try:
        client_uen = state.get("client_uen") or "unknown"
        region = state.get("region") or "SINGAPORE"
        base_currency = state.get("base_currency") or "SGD"
        tax_registered = bool(state.get("tax_registered"))
        fye_month = state.get("fye_month") or "unknown"
    except Exception:  # noqa: BLE001
        return _BASE_INSTRUCTION

    gst_label = "yes" if tax_registered else "no"
    preamble = (
        f"You are working for {client_name} (UEN {client_uen}, {region}, "
        f"base currency {base_currency}, GST-registered: {gst_label}, "
        f"FYE month {fye_month}).\n\n"
    )
    return preamble + _BASE_INSTRUCTION


assistant_agent = LlmAgent(
    name="assistant",
    model=config.MODEL_LITE,
    instruction=assistant_instruction,
    tools=[
        bank_totals,
        summarize_by_category,
        pnl_for_fy,
        gst_threshold_check,
        show_client_profile,
        show_learned_mappings,
        model_info,
        explain_categorization,
        explain_tax_treatment,
        summarize_recent_activity,
        lookup_row,
        list_recent_documents,
    ],
)

__all__ = [
    "assistant_agent",
    "assistant_instruction",
    "LEDGER_DATA_KEY",
    "GST_THRESHOLD_SGD",
    "bank_totals",
    "summarize_by_category",
    "pnl_for_fy",
    "gst_threshold_check",
    "show_client_profile",
    "show_learned_mappings",
    "model_info",
    "explain_categorization",
    "explain_tax_treatment",
    "summarize_recent_activity",
    "lookup_row",
    "list_recent_documents",
]
