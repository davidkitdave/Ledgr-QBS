"""Read-only Q&A LlmAgent over the client's FY ledger.

The agent answers questions about the client's books using pure, deterministic
function tools that operate on ``state["ledger_data"]`` — a list of row dicts
injected by the Slack runner before the graph runs.  NO Slack or network calls
happen inside the tools; the runner layer owns data fetching.

State contract
--------------
``state["ledger_data"]`` : list[dict]
    Each dict is one ledger row with string keys matching the workbook column
    headers (e.g. "Account Code / COA", "Source Amount", "Date", "Doc Type",
    "Tax Rate", ...).  The runner injects this before running the coordinator.
    If the key is absent or the list is empty the agent tells the user the
    ledger is not loaded yet rather than hallucinating.

Tools
-----
- ``summarize_by_category``   — total spend per COA / category
- ``pnl_for_fy``              — revenue minus expenses over all rows
- ``gst_threshold_check``     — compare total taxable turnover to SGD 1 M

All tools are pure (no I/O, no randomness) so they are trivially testable.
"""

from __future__ import annotations

import json

from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext

from . import config

# --------------------------------------------------------------------------- #
# Pure ledger tools (operate on rows already in session state)
# --------------------------------------------------------------------------- #

#: The session state key the runner must set before routing to the Q&A path.
LEDGER_DATA_KEY = "ledger_data"

#: The session state key carrying the user's raw question. The runner sets this
#: alongside ``ledger_data``. It is needed because ``qa_agent`` runs in
#: ``single_turn`` mode (ADK forces ``include_contents='none'``), so the agent
#: cannot see the user turn from history — and the graph delivers only the
#: router's ``{"intent": "question"}`` payload as ``node_input``. Without this
#: the model never sees the actual question and falls back to listing its tools.
QUESTION_KEY = "question_text"

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

    withdrawals = deposits = 0.0
    txn_count = 0
    opening_balance: float | None = None
    closing_balance: float | None = None
    currency = "SGD"

    for row in rows:
        desc = str(row.get("Description") or "").strip().upper()
        if row.get("Currency"):
            currency = row["Currency"]

        # Opening balance comes from the BALANCE B/F marker; skip it from sums.
        if desc == "BALANCE B/F":
            if opening_balance is None:
                opening_balance = _to_float(row.get("Balance"))
            continue
        if desc == "TOTALS":
            continue

        if want_month is not None or want_year is not None:
            mth, yr = _month_year_of(row.get("Date"))
            if want_month is not None and mth != want_month:
                continue
            if want_year is not None and yr != want_year:
                continue

        withdrawals += _to_float(row.get("Withdrawal"))
        deposits += _to_float(row.get("Deposit"))
        txn_count += 1
        bal = row.get("Balance")
        if bal is not None and str(bal).strip() != "":
            closing_balance = _to_float(bal)

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
# Q&A LlmAgent
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

For every question, call the single most relevant tool first, then explain the
result in plain English. For a bank question naming a month, pass that month
(and year if given) to ``bank_totals``. If a tool reports that the data is not
loaded, tell the user to upload the relevant workbook first.

Be concise and professional. Do not invent figures not returned by the tools.
""".strip()


def qa_instruction(ctx) -> str:
    """Build the Q&A system prompt, embedding the user's actual question.

    ``qa_agent`` runs single-turn so the model cannot see the user turn from
    history; the graph only delivers the router's ``{"intent": "question"}``
    payload as ``node_input``. We therefore read the raw question from
    ``state[QUESTION_KEY]`` (set by the runner) and embed it in the system
    prompt, which is always sent regardless of ``include_contents``. Falls back
    to the base instruction when no question is present so document/other lanes
    (which never render this) and tests stay robust.
    """
    try:
        question = (ctx.state.get(QUESTION_KEY) or "").strip()
    except Exception:  # noqa: BLE001 — never let prompt assembly crash the lane
        question = ""
    if not question:
        return _BASE_INSTRUCTION
    return (
        f"{_BASE_INSTRUCTION}\n\n"
        f"The user has asked this question:\n\"\"\"\n{question}\n\"\"\"\n"
        "Answer THIS question now: call the most relevant tool first, then give a "
        "concise plain-English answer grounded in the tool result. Do NOT reply "
        "with a generic list of what you can do."
    )


qa_agent = LlmAgent(
    name="qa_agent",
    model=config.MODEL_LITE,
    mode="single_turn",
    instruction=qa_instruction,
    tools=[bank_totals, summarize_by_category, pnl_for_fy, gst_threshold_check],
)

__all__ = [
    "qa_agent",
    "qa_instruction",
    "LEDGER_DATA_KEY",
    "QUESTION_KEY",
    "GST_THRESHOLD_SGD",
    "bank_totals",
    "summarize_by_category",
    "pnl_for_fy",
    "gst_threshold_check",
]
