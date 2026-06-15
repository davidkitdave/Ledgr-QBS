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

import hashlib
import json
import logging
from datetime import date, datetime, timedelta

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool, ToolContext

from invoice_processing.export.categorizer import resolve_account
from invoice_processing.export.client_context import (
    category_mapping_from_state,
    coa_from_state,
    entity_memory_from_state,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from invoice_processing.export.tax_classifier import TaxClassifier

from . import config

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Pure ledger tools (operate on rows already in session state)
# --------------------------------------------------------------------------- #

#: The session state key the runner must set before routing to the chat path.
LEDGER_DATA_KEY = "ledger_data"

#: The session state key the chat write tools append confirmed write specs to.
#: The Slack runner drains this AFTER a chat turn, executing each spec against
#: the workbook (the tools never do network I/O themselves — see ADR-0009).
PENDING_WRITE_KEY = "pending_ledger_write"

#: The session state key the learn_mapping tool appends mapping specs to.
#: The Slack runner drains this AFTER a chat turn, calling
#: ``client_store.add_correction`` for each entry (the tool never does I/O).
PENDING_LEARN_KEY = "pending_learn_mapping"

#: Invoice sheets the write tools may mutate. Bank sheets carry a derived running
#: balance (memory ``bank-ledger-continuous-sorted``) so amending/removing one
#: would desync the chain — the tools refuse with a clear message instead.
_INVOICE_SHEETS: frozenset[str] = frozenset({"Purchase", "Sales"})

#: The accounting software value that the write tools support.  Xero workbooks
#: use different column headers (``*AccountCode``, ``TaxAmount`` no-space, etc.)
#: so editing them from chat would silently write wrong data or raise "unknown
#: column" errors. Gate the tools to QBS now; Xero support is a follow-on.
_SUPPORTED_WRITE_SOFTWARE: frozenset[str] = frozenset({"QBS Ledger", "qbs"})

#: User-facing amend field → the canonical workbook column header (QBS Ledger).
#: ``tax`` is handled specially (it re-derives the QBS ``Tax Amount`` via the
#: classifier, never a free-text write), so it is intentionally absent here.
_EDITABLE_FIELD_HEADERS: dict[str, str] = {
    "account": "Account Code / COA",
    "account code": "Account Code / COA",
    "coa": "Account Code / COA",
    "amount": "Source Amount",
    "net amount": "Source Amount",
    "description": "Description",
}

#: Field aliases that mean "amend the tax treatment". These pass THROUGH the
#: §0.5-C master gate (a non-registered client is forced to NT) rather than
#: writing the user's literal text.
_TAX_FIELD_ALIASES: frozenset[str] = frozenset(
    {"tax", "tax rate", "tax treatment", "tax code", "tax type"}
)

#: Dollar-amount tax headers that the QBS layout uses.  ``TaxAmount`` (no
#: space) is the Xero column — kept here defensively so a mixed workbook never
#: leaves a stale dollar value, but QBS clients are already gated above.
#: ``Tax Rate`` / ``tax_rate`` are NOT live workbook columns and are removed
#: to avoid writing a raw canonical treatment code into a wrong cell.
_TAX_AMOUNT_HEADERS: frozenset[str] = frozenset({"Tax Amount", "TaxAmount"})
#: Code-carrying headers rewritten from the re-classified treatment.
#: Only ``*TaxType`` (Xero) remains; dead ``Tax Rate``/``tax_rate`` entries removed.
_TAX_CODE_HEADERS: frozenset[str] = frozenset({"*TaxType"})

#: Columns used to build a row SIGNATURE for replay-safety (HIGH-2).  The
#: signature is a stable hash of key identifying values captured at Turn-1 so
#: the commit branch can detect that the row shifted or was edited since the
#: user saw the proposal.
_SIGNATURE_COLS: tuple[str, ...] = (
    "Description", "Source Amount", "Account Code / COA", "Tax Amount",
)

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
# Write tools (Step 4 / C-2) — gated behind ADK Tool Confirmation (ADR-0009)
# --------------------------------------------------------------------------- #


def _row_doc_type(row: dict) -> str:
    """Map a ledger row's sheet/Doc Type to the classifier ``doc_type``."""
    sheet = str(row.get("_sheet") or "").strip()
    if sheet == "Sales":
        return "sales"
    if sheet == "Purchase":
        return "purchase"
    dt = str(row.get("Doc Type") or "").strip().upper()
    return "sales" if dt == "S" else "purchase"


def _reclassify_tax_for_row(
    row: dict, *, registered: bool, tax_keyword: str | None = None
) -> tuple[str, dict]:
    """Re-run the §0.5-C tax classifier for ``row`` and derive its tax columns.

    Reconstructs a one-line ``InvoiceLine`` on a ``NormalizedInvoice`` whose
    ``our_gst_registered`` comes from the CLIENT PROFILE (``registered``), NOT
    the row and NOT the user — so the master gate is re-applied (a non-registered
    client is forced to ``NT`` even if the user asked for ``SR``).

    ``tax_keyword`` (set only when the user is explicitly amending the tax
    treatment) is fed in as the line's explicit tax hint so the classifier
    honours the requested code for a registered client; the master gate still
    overrides it to ``NT`` for a non-registered client.

    Returns ``(treatment, tax_column_updates)`` where ``tax_column_updates`` maps
    the workbook tax headers present on ``row`` to their re-derived values
    (``Tax Amount`` dollar value for QBS; ``Tax Rate`` / ``*TaxType`` code for
    code-carrying layouts).
    """
    doc_type = _row_doc_type(row)
    net = _to_float(row.get("Source Amount") or row.get("Sub Total") or row.get("amount"))
    gst_cell = row.get("Tax Amount")
    gst = _to_float(gst_cell) if gst_cell not in (None, "") else None
    inv_date = _parse_row_date(row.get("Invoice Date") or row.get("Date"))

    line = InvoiceLine(
        description=str(row.get("Description") or ""),
        net_amount=net,
        gst_amount=gst,
        tax_keyword=(tax_keyword or "").strip() or None,
    )
    inv = NormalizedInvoice(
        doc_type=doc_type,
        invoice_date=inv_date,
        supplier=PartyInfo(name="Supplier", country="SG"),
        customer=PartyInfo(name="Customer", country="SG"),
        our_gst_registered=registered,
    )
    clf = TaxClassifier()
    clf.classify_line(line, inv)

    updates: dict = {}
    for header in _TAX_AMOUNT_HEADERS:
        if header in row:
            # QBS Tax Amount: GST dollars (only SR carries tax; else 0).
            if line.tax_treatment == "SR":
                rate = clf.rate_for_date(inv_date)
                amt = line.gst_amount if line.gst_amount else net * rate
                updates[header] = round(float(amt or 0.0), 2)
            else:
                updates[header] = 0.0
    for header in _TAX_CODE_HEADERS:
        if header in row:
            if header == "*TaxType":
                updates[header] = clf.tax_code(line.tax_treatment, doc_type, "xero")
            else:
                updates[header] = line.tax_treatment
    return line.tax_treatment, updates


def _row_signature(row: dict) -> str:
    """Return a stable hash of the row's key identifying values.

    Captured at Turn-1 (proposal) and stored in the write spec. Verified at
    Turn-2 / replay by re-reading the workbook row at the same (sheet, row)
    coordinate and comparing — if the content shifted (row deletion upstream,
    concurrent edit, or a replay after a partial failure) the write is refused
    rather than silently corrupting a now-different row.
    """
    sig_values = "|".join(
        str(row.get(col, "")) for col in _SIGNATURE_COLS
    )
    return hashlib.sha256(sig_values.encode()).hexdigest()[:16]


def _load_target_row(
    tool_context: ToolContext, row_index: str
) -> tuple[dict | None, str]:
    """Resolve ``row_index`` against ``state["ledger_data"]``.

    Returns ``(row, "")`` on success or ``(None, message)`` with a plain
    explanatory refusal string the tool returns verbatim.  Guards checked in
    order: ledger not loaded → unknown index → bank sheet → non-QBS software.
    """
    rows = _get_rows(tool_context)
    if not rows:
        return None, "The ledger data is not loaded yet. Please upload the FY ledger first."

    try:
        idx = int(str(row_index).strip())
    except (TypeError, ValueError):
        return None, (
            f"I couldn't read the row reference {row_index!r}. Use lookup_row first to "
            "get the row_index of the line you mean."
        )
    if idx < 0 or idx >= len(rows):
        return None, (
            f"There's no row {idx} in the loaded ledger (it has {len(rows)} rows). "
            "Use lookup_row to find the right row first."
        )

    row = rows[idx]
    sheet = str(row.get("_sheet") or "")
    if sheet not in _INVOICE_SHEETS:
        return None, (
            f"That row is on the bank sheet ({sheet or 'bank'}), which is read-only "
            "from chat — its running balance is derived, so editing one line would "
            "desync the balances. I can only amend or remove invoice ledger rows "
            "(Purchase / Sales)."
        )

    # Gate: non-QBS workbook layouts use different column headers (e.g. Xero
    # uses ``*AccountCode`` / ``TaxAmount`` no-space).  Writing to them via the
    # QBS-shaped edit logic would silently produce wrong tax dollars or raise
    # "unknown column" errors.  Refuse with a clear message rather than corrupt
    # the workbook — Xero write support is a follow-on task.
    software = str(tool_context.state.get("software") or "").strip()
    if software and software not in _SUPPORTED_WRITE_SOFTWARE:
        return None, (
            f"Editing this ledger layout ({software!r}) from chat isn't supported yet "
            "— only QBS Ledger workbooks can be amended here. "
            "Use your accounting software to make this change."
        )

    return row, ""


def _build_amend_spec(
    tool_context: ToolContext,
    row: dict,
    field: str,
    new_value: str,
) -> tuple[dict, str, str]:
    """Deterministically build the canonical amend write spec from the tool args.

    Pure with respect to the inputs (``row`` + args + ``state["tax_registered"]``):
    given the SAME row and args it returns the SAME spec on every call. This is the
    seam that makes Turn-1 (preview) and Turn-2 (commit) identical BY CONSTRUCTION —
    the commit does NOT depend on ADK carrying the Turn-1 ``request_confirmation``
    payload through to the Turn-2 ``ToolConfirmation`` (which ADK does not reliably
    do; see ADR-0009 / the e2e test). §0.5-C re-runs the tax classifier with
    ``registered`` from the CLIENT PROFILE so a non-registered client is forced to
    ``NT`` exactly as previewed.

    Returns ``(spec, hint, treatment)`` — ``hint`` for the confirmation prompt,
    ``treatment`` for the re-derived tax treatment.
    """
    try:
        registered = bool(tool_context.state.get("tax_registered", True))
    except Exception:  # noqa: BLE001
        registered = True

    field_key = (field or "").strip().lower()
    is_tax = field_key in _TAX_FIELD_ALIASES
    header = _EDITABLE_FIELD_HEADERS.get(field_key)

    updates: dict = {}
    # Build a working copy of the row to reflect the user's edit before
    # re-classifying tax (so account/amount changes re-derive tax too).
    working = dict(row)
    requested_kw: str | None = None
    if is_tax:
        # Amending tax: feed the requested treatment through the master gate
        # as the line's explicit tax_keyword, then re-classify — which forces
        # NT for a non-registered client. Clear the dollar Tax Amount so the
        # classifier derives it from net*rate for the new treatment.
        requested_kw = (new_value or "").strip()
        working["Tax Amount"] = None
    else:
        updates[header] = new_value
        working[header] = new_value

    treatment, tax_updates = _reclassify_tax_for_row(
        working, registered=registered, tax_keyword=requested_kw
    )
    updates.update(tax_updates)

    before = {col: row.get(col) for col in updates}
    diff_lines = [
        f"• {col}: {before.get(col)!r} → {new!r}" for col, new in updates.items()
    ]
    gate_note = ""
    if is_tax and not registered:
        gate_note = (
            "\n(Client is NOT GST-registered, so the tax treatment is forced "
            f"to {treatment} regardless of the requested value.)"
        )
    hint = (
        f"Amend {row.get('_sheet')} row {row.get('_row')} "
        f"({row.get('Description') or 'this line'}):\n"
        + "\n".join(diff_lines)
        + gate_note
        + "\n\nReply 'yes' to apply, or 'no' to cancel."
    )
    spec = {
        "op": "amend",
        "sheet": row.get("_sheet"),
        "row": row.get("_row"),
        "updates": updates,
        "tax_treatment": treatment,
        # Replay-safety (HIGH-2): a hash of the row's key column values at
        # proposal time.  The runner re-reads the row before writing and
        # refuses if the signature no longer matches — catches row shifts
        # (upstream deletion) and concurrent edits.
        "row_signature": _row_signature(row),
    }
    return spec, hint, treatment


def _build_remove_spec(row: dict) -> tuple[dict, str]:
    """Deterministically build the canonical remove write spec from ``row``.

    Same payload-independence rationale as :func:`_build_amend_spec`: Turn-1 and
    Turn-2 both derive the spec from the same row, so the commit never relies on
    ADK echoing the Turn-1 confirmation payload. Returns ``(spec, hint)``.
    """
    desc = row.get("Description") or "this line"
    amount = row.get("Source Amount") or row.get("amount")
    hint = (
        f"Remove {row.get('_sheet')} row {row.get('_row')} — "
        f"{desc} ({amount})?\n\nReply 'yes' to delete it, or 'no' to keep it."
    )
    spec = {
        "op": "remove",
        "sheet": row.get("_sheet"),
        "row": row.get("_row"),
        # Replay-safety: same signature scheme as amend_ledger_row.
        "row_signature": _row_signature(row),
    }
    return spec, hint


def amend_ledger_row(
    tool_context: ToolContext,
    row_index: str,
    field: str,
    new_value: str,
) -> str:
    """Amend one field of an invoice ledger row (gated — asks you to confirm first).

    Two-turn confirm (ADR-0009): the FIRST call previews the before→after change
    (including the §0.5-C re-classified tax) and asks for your OK; it writes
    nothing. After you confirm, the change is committed. Call ``lookup_row``
    FIRST to get the ``row_index`` of the line you mean.

    Args:
        tool_context: Injected by ADK; provides session state + confirmation.
        row_index: Index into the loaded ledger (as returned by ``lookup_row``).
        field: Which field to change — ``account`` / ``amount`` / ``description``
            / ``tax``.
        new_value: The new value (for ``tax`` this is a requested treatment; a
            non-registered client is still forced to ``NT`` by the master gate).

    Returns:
        A short status string. The human-readable diff is surfaced via the
        confirmation hint; the commit appends a write spec to
        ``state["pending_ledger_write"]`` for the runner to execute.
    """
    # Guards (ledger loaded, known row, invoice sheet, QBS software) run on
    # BOTH turns — re-deriving naturally re-runs them on Turn-2.
    row, refusal = _load_target_row(tool_context, row_index)
    if row is None:
        return refusal

    field_key = (field or "").strip().lower()
    is_tax = field_key in _TAX_FIELD_ALIASES
    header = _EDITABLE_FIELD_HEADERS.get(field_key)
    if not is_tax and header is None:
        allowed = "account, tax, amount, description"
        return (
            f"I can't amend {field!r}. Editable fields are: {allowed}."
        )

    confirmation = getattr(tool_context, "tool_confirmation", None)

    # ----- Turn 1: build the preview + request confirmation -----
    if not confirmation:
        spec, hint, _treatment = _build_amend_spec(
            tool_context, row, field, new_value
        )
        try:
            # payload is passed for audit/UI only — the commit re-derives the
            # spec from the original args and does NOT rely on it returning.
            tool_context.request_confirmation(hint=hint, payload=spec)
        except Exception:  # noqa: BLE001 — never let the gate crash the lane
            logger.exception(
                "amend_ledger_row: request_confirmation failed "
                "(sheet=%s row=%s) — ADK Tool Confirmation may have regressed",
                row.get("_sheet"), row.get("_row"),
            )
            return "I couldn't open the confirmation step. Please try again."
        return "Awaiting your confirmation before I change the ledger."

    # ----- Turn 2: the user answered -----
    if not getattr(confirmation, "confirmed", False):
        return "Okay, I won't change anything."

    # Re-derive the write spec from the SAME original args (ADK re-invokes the
    # tool with the original call's args on resume). Identical deterministic
    # computation as Turn-1 → preview == commit by construction. We use
    # ``confirmation.confirmed`` ONLY for the yes/no — never its payload.
    spec, _hint, _treatment = _build_amend_spec(
        tool_context, row, field, new_value
    )
    pending = tool_context.state.get(PENDING_WRITE_KEY)
    if not isinstance(pending, list):
        pending = []
    pending.append(spec)
    tool_context.state[PENDING_WRITE_KEY] = pending
    return "Confirmed — applying the change to your ledger now."


def remove_ledger_row(tool_context: ToolContext, row_index: str) -> str:
    """Remove an invoice ledger row (gated — asks you to confirm first).

    Two-turn confirm (ADR-0009): the FIRST call previews the row to be removed
    and asks for your OK; it writes nothing. After you confirm, the row is
    deleted. Call ``lookup_row`` FIRST to get the ``row_index``.

    Args:
        tool_context: Injected by ADK; provides session state + confirmation.
        row_index: Index into the loaded ledger (as returned by ``lookup_row``).

    Returns:
        A short status string. The commit appends a write spec to
        ``state["pending_ledger_write"]`` for the runner to execute.
    """
    # Guards run on BOTH turns (re-derivation re-runs _load_target_row).
    row, refusal = _load_target_row(tool_context, row_index)
    if row is None:
        return refusal

    confirmation = getattr(tool_context, "tool_confirmation", None)

    if not confirmation:
        spec, hint = _build_remove_spec(row)
        try:
            # payload is for audit/UI only; Turn-2 re-derives from the args.
            tool_context.request_confirmation(hint=hint, payload=spec)
        except Exception:  # noqa: BLE001
            logger.exception(
                "remove_ledger_row: request_confirmation failed "
                "(sheet=%s row=%s) — ADK Tool Confirmation may have regressed",
                row.get("_sheet"), row.get("_row"),
            )
            return "I couldn't open the confirmation step. Please try again."
        return "Awaiting your confirmation before I remove the row."

    if not getattr(confirmation, "confirmed", False):
        return "Okay, I won't remove anything."

    # Re-derive from the same original args; never rely on confirmation.payload.
    spec, _hint = _build_remove_spec(row)
    pending = tool_context.state.get(PENDING_WRITE_KEY)
    if not isinstance(pending, list):
        pending = []
    pending.append(spec)
    tool_context.state[PENDING_WRITE_KEY] = pending
    return "Confirmed — removing that row from your ledger now."


# --------------------------------------------------------------------------- #
# Learn-mapping write tool (Step 7 / C-3) — direct write, no confirmation gate
# --------------------------------------------------------------------------- #


def learn_mapping(
    tool_context: ToolContext,
    vendor: str,
    account_code: str = "",
    tax_code: str = "",
) -> str:
    """Teach the assistant a vendor→account or vendor→tax rule for future invoices.

    When you say "remember, Vendor X goes to account 6090" or "Vendor Y is
    always ZR", this tool records the rule in entity_memory so the next invoice
    from that vendor is auto-categorised correctly.

    This is a DIRECT write (no confirmation step) — the user's imperative IS
    the human action (ADR-0004).

    Args:
        tool_context: Injected by ADK; provides session state.
        vendor: The vendor / supplier name to map (required).
        account_code: The COA account code to assign (e.g. ``6090``). At least
            one of ``account_code`` / ``tax_code`` must be provided.
        tax_code: The tax treatment to assign (e.g. ``SR``, ``ZR``, ``NT``).
            At least one of ``account_code`` / ``tax_code`` must be provided.

    Returns:
        A confirmation message naming what was learned, or a plain-English
        rejection explaining what was wrong.
    """
    v = (vendor or "").strip()
    if not v:
        return (
            "I need a vendor name to learn a mapping. "
            "Try: \"remember, Acme goes to account 6090\"."
        )

    ac = (account_code or "").strip()
    tc = (tax_code or "").strip()

    if not ac and not tc:
        return (
            f"Please tell me what to map {v!r} to — "
            "an account code (e.g. 6090), a tax code (e.g. SR / ZR), or both."
        )

    # Validate account_code against the client's COA when one is supplied.
    if ac:
        try:
            coa = tool_context.state.get("coa") or []
        except Exception:  # noqa: BLE001
            coa = []
        if coa:
            # COA entries may be dicts ({"code": "6090", ...}) or plain strings.
            known_codes: set[str] = set()
            for entry in coa:
                if isinstance(entry, dict):
                    code = str(entry.get("code") or entry.get("account_code") or "").strip()
                    if code:
                        known_codes.add(code)
                elif isinstance(entry, str):
                    known_codes.add(entry.strip())
            if known_codes and ac not in known_codes:
                return (
                    f"I don't recognise {ac!r} in this client's chart of accounts. "
                    "Check the code and try again (use ``show_learned_mappings`` to "
                    "see what accounts are available)."
                )

    # Append the mapping spec to the pending list — the runner drains it post-run.
    try:
        pending = tool_context.state.get(PENDING_LEARN_KEY)
        if not isinstance(pending, list):
            pending = []
        pending.append({
            "vendor": v,
            "account_code": ac or None,
            "tax_code": tc or None,
        })
        tool_context.state[PENDING_LEARN_KEY] = pending
    except Exception:  # noqa: BLE001 — never crash the lane
        logger.exception("learn_mapping: failed to append pending entry for vendor=%r", v)
        return "Something went wrong recording that mapping — please try again."

    parts: list[str] = []
    if ac:
        parts.append(f"account {ac}")
    if tc:
        parts.append(f"tax code {tc}")
    mapping_desc = " and ".join(parts)
    return (
        f"Got it — I'll code invoices from {v} to {mapping_desc} from now on."
    )


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

Write tools (when the user asks you to FIX or DELETE a ledger line):
- ``amend_ledger_row``: change one field (``account`` / ``amount`` / ``description``
  / ``tax``) of an invoice ledger row.
- ``remove_ledger_row``: delete an invoice ledger row.
BEFORE calling either write tool you MUST call ``lookup_row`` to get the exact
``row_index`` of the line the user means — never guess an index. Both write
tools are GATED: the first call only PROPOSES the change and asks the user to
confirm; nothing is written until the user replies "yes". Only invoice rows
(Purchase / Sales) can be edited — bank rows are read-only. The tax treatment is
always re-derived by the engine (a non-GST-registered client is forced to NT),
so do not promise a specific tax code the user typed.

Learning tool (when the user says "remember X goes to Y" or "always code X as Y"):
- ``learn_mapping``: record a vendor→account or vendor→tax rule so the next
  invoice from that vendor is auto-categorised correctly. This is IMMEDIATE —
  no confirmation step. Call it as soon as you recognise the user's intent to
  teach a rule. Confirm back what was learned in plain English.

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
        FunctionTool(amend_ledger_row, require_confirmation=True),
        FunctionTool(remove_ledger_row, require_confirmation=True),
        learn_mapping,
    ],
)

__all__ = [
    "assistant_agent",
    "assistant_instruction",
    "LEDGER_DATA_KEY",
    "PENDING_WRITE_KEY",
    "PENDING_LEARN_KEY",
    "GST_THRESHOLD_SGD",
    "amend_ledger_row",
    "remove_ledger_row",
    "learn_mapping",
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
