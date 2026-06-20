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
import os
from datetime import date, datetime, timedelta

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.tools import FunctionTool, ToolContext

from invoice_processing.export.categorizer import resolve_account
from invoice_processing.export.client_context import (
    category_mapping_from_state,
    coa_from_state,
    entity_memory_from_state,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice

from . import config
from .jurisdiction import (
    REGION_MALAYSIA,
    REGION_SINGAPORE,
    _norm_region,
    _resolve_client_currency,
    registration_threshold_for_region,
    resolve_jurisdiction,
    write_to_state,
)
from .tax_reasoning import reason_one_invoice as _reason_one_invoice

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

#: The session state key the ``re_extract_document`` tool appends re-extract
#: specs to (ADR-0010). The Slack runner drains this AFTER a chat turn, re-running
#: the document pipeline (with the hint + ``replace=True``) per spec via
#: ``process_file_event`` — the tool itself never downloads or runs anything.
PENDING_REEXTRACT_KEY = "pending_reextract"

#: Recent document-processing deliveries injected by the Slack runner (ADR chat introspection).
PROCESSING_LOG_KEY = "processing_log"

#: Pending HITL reviews for the current channel. Injected by the runner from
#: ``hitl.list_pending_interrupts`` so the chat agent can answer "anything
#: waiting on my approval?" without doing its own Firestore I/O.
PENDING_REVIEWS_KEY = "pending_reviews"

#: Per-document session snapshot for files referenced in the processing log.
#: Injected by the runner via ``_snapshot_doc_sessions``; the chat tools
#: treat this as read-only introspection data only.
DOCUMENT_SESSIONS_KEY = "document_sessions"

#: Last invoice/account-code focus for thread follow-ups (set by the runner
#: after a direct account-code answer or ``explain_posted_line``).
THREAD_FOCUS_KEY = "thread_focus"



#: Invoice sheets the write tools may mutate. Bank sheets carry a derived running
#: balance (memory ``bank-ledger-continuous-sorted``) so amending/removing one
#: would desync the chain — the tools refuse with a clear message instead.
_INVOICE_SHEETS: frozenset[str] = frozenset({"Purchase", "Sales"})

#: The accounting software whose workbooks the chat AMEND/edit tools may write to.
#: This gates ONLY in-chat editing of an existing workbook — NOT export. Xero is
#: fully supported for EXPORT (see ``XeroLedgerExporter`` / ``EXPORTERS``): we
#: generate correct Xero import rows from scratch. But the amend tools edit existing
#: rows by QBS column header (``_EDITABLE_FIELD_HEADERS``); Xero uses different
#: headers (``*AccountCode``, ``TaxAmount`` no-space), so amending a Xero workbook
#: through the QBS-shaped edit path would silently write wrong tax dollars or raise
#: "unknown column" errors. Keep the amend gate until the amend tools are made
#: column-aware per software (deliberate safety guard, not rigid over-control —
#: WS4.3 decision 2026-06-19).
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

#: Legacy re-exports — canonical values live in jurisdiction YAML ``registration_threshold``.
GST_THRESHOLD_SGD, _, _ = registration_threshold_for_region(REGION_SINGAPORE)
SST_THRESHOLD_MYR, _, _ = registration_threshold_for_region(REGION_MALAYSIA)


def _build_resolver_state(state: dict) -> dict:
    """Build jurisdiction resolver input — derive currency from registry, never default SG."""
    resolver_state = dict(state)
    region = _norm_region(
        resolver_state.get("client_region") or resolver_state.get("region") or ""
    )
    if region and not resolver_state.get("base_currency"):
        currency = _resolve_client_currency(resolver_state, region)
        if currency:
            resolver_state["base_currency"] = currency
    return resolver_state


def _tax_registration_threshold(state: dict) -> tuple[float, str, str]:
    """Return (threshold_amount, currency, label) for the active jurisdiction.

    Reads region from ``state["region"]`` (canonical) or ``state["client_region"]``
    (legacy). Falls back to SG / SGD / 1_000_000 when no region is present so
    legacy SG clients see no behaviour change.

    Order of precedence:
    1. Env override ``LEDGR_TAX_REGISTRATION_THRESHOLD_<REGION>`` (most explicit).
    2. Per-region ``registration_threshold`` from the jurisdiction YAML.
    3. SG / 1M SGD (legacy fallback — never silently for a non-SG client).
    """
    region = _norm_region(state.get("client_region") or state.get("region") or "")
    if region == REGION_SINGAPORE:
        amount, currency, label = registration_threshold_for_region(REGION_SINGAPORE)
        threshold = float(
            os.environ.get("LEDGR_TAX_REGISTRATION_THRESHOLD_SG", amount)
        )
        return threshold, currency, label
    if region == REGION_MALAYSIA:
        amount, currency, label = registration_threshold_for_region(REGION_MALAYSIA)
        threshold = float(
            os.environ.get("LEDGR_TAX_REGISTRATION_THRESHOLD_MY", amount)
        )
        return threshold, currency, label
    amount, currency, label = registration_threshold_for_region(REGION_SINGAPORE)
    return float(amount), currency, f"{label} (fallback)"


def _normalize_row_for_tools(row: dict) -> dict:
    """Return a shallow-copied row with Xero columns aliased to QBS column names.

    QBS export uses ``Source Filename`` / ``Doc Type`` / ``Source Amount`` /
    ``Description`` / ``Account Code / COA`` / ``Date``. Xero export uses
    ``*ContactName`` / ``*InvoiceNumber`` / ``*UnitAmount`` / ``*Description``
    / ``*AccountCode`` / ``*InvoiceDate``. The chat tools all read the QBS
    column names, so without normalization a Xero ledger would return
    ``filename="unknown"`` for every row (see ADR-0010: workbook rows are
    anonymous — there is no source-file column). Aliasing the invoice number
    into ``Source Filename`` is a pragmatic grouping key (the file is
    identified in ``processing_log`` instead).

    The original row is left untouched (defensive copy) so any caller holding
    a reference to the underlying list still sees canonical Xero columns if
    it needs them.
    """
    if not isinstance(row, dict):
        return row
    out = dict(row)
    # Source Filename: Xero rows have no file id; group by invoice number.
    if not out.get("Source Filename") and not out.get("source_filename"):
        inv = (
            out.get("*InvoiceNumber")
            or out.get("*Reference")
            or out.get("Reference")
        )
        if inv:
            out["Source Filename"] = f"Xero:{inv}"
    # Doc Type: infer from sheet when absent.
    if not out.get("Doc Type"):
        sheet = str(out.get("_sheet") or "").strip().lower()
        if sheet == "purchase":
            out["Doc Type"] = "Purchase"
        elif sheet == "sales":
            out["Doc Type"] = "Sales"
    # Source Amount: QBS field. Xero uses *UnitAmount (per-line) and Amount
    # (per-invoice total). Prefer the explicit per-line amount; fall back to
    # Amount so at least the headline value surfaces.
    if not out.get("Source Amount") and not out.get("amount"):
        amount = out.get("*UnitAmount") or out.get("Amount")
        if amount is not None:
            out["Source Amount"] = amount
    # Description.
    if not out.get("Description") and not out.get("description"):
        desc = out.get("*Description")
        if desc is not None:
            out["Description"] = desc
    # Account Code / COA.
    if not out.get("Account Code / COA") and not out.get("account_code"):
        acct = out.get("*AccountCode")
        if acct is not None:
            out["Account Code / COA"] = acct
    # Date.
    if not out.get("Date") and not out.get("date"):
        d = out.get("*InvoiceDate")
        if d is not None:
            out["Date"] = d
    # Vendor / contact (Xero *ContactName).
    if not out.get("Vendor") and not out.get("vendor"):
        contact = out.get("*ContactName")
        if contact is not None:
            out["Vendor"] = contact
    return out


def _get_rows(tool_context: ToolContext) -> list[dict]:
    """Return the ledger rows from session state (empty list if absent).

    Rows are passed through :func:`_normalize_row_for_tools` so Xero and QBS
    layouts look identical to downstream tools.  This is the data-plane fix
    for ADR-0010's "no source-file column" limitation; chat tools that
    previously reported ``filename="unknown"`` for Xero clients now see the
    invoice number as a stable grouping key.
    """
    rows = tool_context.state.get(LEDGER_DATA_KEY)
    if not isinstance(rows, list):
        return []
    return [_normalize_row_for_tools(r) for r in rows]


def _diagnostic_counts(tool_context: ToolContext) -> dict:
    """Return the small set of context numbers the runner injects.

    Pulled from ``state`` (filled by the Slack runner) so empty-state messages
    can name the FY, the row count, the processing-log depth, and the
    pending-review count instead of saying "upload the ledger first" with
    no context.
    """
    state = tool_context.state
    fy = state.get("fy_loaded") or "unknown"
    try:
        rows = int(state.get("ledger_row_count") or 0)
    except (TypeError, ValueError):
        rows = 0
    try:
        plog_raw = state.get("processing_log_count")
        if plog_raw is not None:
            plog = int(plog_raw)
        else:
            plog = len(state.get(PROCESSING_LOG_KEY) or [])
    except (TypeError, ValueError):
        plog = 0
    try:
        pending_raw = state.get("pending_review_count")
        if pending_raw is not None:
            pending = int(pending_raw)
        else:
            pending = len(state.get(PENDING_REVIEWS_KEY) or [])
    except (TypeError, ValueError):
        pending = 0
    return {
        "fy_loaded": fy,
        "ledger_row_count": rows,
        "processing_log_count": plog,
        "pending_review_count": pending,
        "software": state.get("software") or "",
        "client_name": state.get("client_name") or "",
    }


def _empty_ledger_message(tool_context: ToolContext) -> str:
    """Render a diagnostic empty-state message instead of a generic prompt.

    The chat agent would otherwise tell the user "upload the FY ledger"
    with no idea which FY, how many pointers exist, or how many deliveries
    are on file.  This message names the actual context so the LLM can
    suggest a concrete next step (e.g. "we have FY2026 with 0 rows but
    FY2025 has 42 — ask me about FY2025").
    """
    diag = _diagnostic_counts(tool_context)
    pointers = tool_context.state.get("fy_pointers") or []
    pointer_summary = ""
    if isinstance(pointers, list) and pointers:
        parts = []
        for s in pointers[:6]:
            if not isinstance(s, dict):
                continue
            fy = s.get("fy", "?")
            count = s.get("row_count", 0)
            parts.append(f"FY{fy}={count}")
        if parts:
            pointer_summary = " Pointers: " + ", ".join(parts) + "."
    fy = diag["fy_loaded"]
    return (
        f"The ledger data is not loaded for FY{fy} (row_count=0).{pointer_summary} "
        f"Processing log has {diag['processing_log_count']} entries. "
        "If a different FY has data, ask me to load it explicitly."
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


def _state_to_dict(state) -> dict:
    """Coerce a session state (dict, ADK ``State``, or ``None``) to a plain ``dict``.

    ADK's ``State`` class supports ``__getitem__`` but NOT the iterator protocol
    required by ``dict()`` — calling ``dict(state)`` raises ``KeyError: 0``
    because it probes ``state[0]``, ``state[1]``, ... for integer keys. This
    helper handles both shapes (and ``None``) so callers can treat the result as
    a plain mapping without knowing the ADK runtime's state container.

    The well-known jurisdiction / profile keys we care about are copied
    explicitly; anything else is lost (callers that need full-fidelity should
    pass a dict).
    """
    if state is None:
        return {}
    if isinstance(state, dict):
        return dict(state)
    out: dict = {}
    # ADK State: read the well-known keys we look up downstream via ``get``.
    for key in (
        "region",
        "client_region",
        "base_currency",
        "client_currency",
        "tax_registered",
        "tax_system",
        "tax_system_hint",
        "tax_jurisdiction",
        "client_id",
        "client_name",
        "supplier_country",
        "bill_to_country",
        "invoice_currency",
        "fye_month",
        "software",
        "currency",
    ):
        try:
            if key in state:
                out[key] = state[key]
        except Exception:
            continue
    return out


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


# --------------------------------------------------------------------------- #
# Explain + lookup read tools (Step 3 / C-1)
# --------------------------------------------------------------------------- #


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


def lookup_coa_account(tool_context: ToolContext, account_code: str) -> str:
    """Return COA description, type, and keywords for a posted account code.

    Reads the client's chart of accounts from session state (``coa`` list
    injected by the runner from ``ClientContext``). Use when the user asks
    what a code *means* in their COA — distinct from ``explain_categorization``,
    which re-runs the engine's pick logic for a vendor/line.

    Args:
        tool_context: Injected by ADK; provides session state.
        account_code: The COA code to look up (e.g. ``902-A02``, ``6-3000``).

    Returns:
        JSON with ``status`` ``found`` or ``not_found`` and COA fields when found.
    """
    from accounting_agents.assistant_tools._helpers import find_coa_by_code

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
    the previous SG 9% mismatch that flagged the YAU LEE receipt).

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
    from accounting_agents.assistant_tools._helpers import row_search_text

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
    from accounting_agents.assistant_tools._helpers import filename_matches_query

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


# --------------------------------------------------------------------------- #
# Diagnostic / introspection tools (P1) — read state only, no I/O
# Implementation lives in ``assistant_tools.introspect``; re-exported here
# for the LlmAgent ``tools=[...]`` list and for backward-compatible
# test imports (``from accounting_agents.assistant import ...``).
# --------------------------------------------------------------------------- #
from accounting_agents.assistant_tools.introspect import (  # noqa: E402,F401
    diagnose_assistant_context,
    explain_posted_line,
    get_document_processing_detail,
    list_pending_reviews,
    list_processing_history,
)


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
    row: dict, *, registered: bool, tax_keyword: str | None = None,
    state: dict | None = None,
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

    Multi-country support: jurisdiction is resolved from session ``state``
    (NOT hardcoded SG). The classifier picks the correct rate band:
    SG 9% GST or MY 8% SST. Python only does the math guard.

    Returns ``(treatment, tax_column_updates)`` where ``tax_column_updates`` maps
    the workbook tax headers present on ``row`` to their re-derived values
    (``Tax Amount`` dollar value for QBS; ``Tax Rate`` / ``*TaxType`` code for
    code-carrying layouts).
    """
    from invoice_processing.export.tax_classifier import TaxClassifier

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
        our_gst_registered=registered,
    )
    # Resolve jurisdiction from session state — no silent SG/SGD injection (C10).
    resolver_state = _build_resolver_state(_state_to_dict(state))
    resolution = resolve_jurisdiction(resolver_state)
    write_to_state(resolver_state, resolution)
    if resolution.jurisdiction.code == "SINGAPORE":
        # Local import keeps the SG-only classifier confined to the SG
        # branch; the chat agent at large no longer imports it at module
        # top level (chat-no-engine-import task).
        clf = TaxClassifier()
        clf.classify_line(line, inv)
    else:
        _reason_one_invoice(inv, state=resolver_state, jurisdiction_resolution=resolution)
    # tax_code resolution: SG via classifier.tax_code; MY / cross-border
    # via the per-jurisdiction code_map from the reference YAML.
    tax_code_for = _resolve_tax_code(line.tax_treatment, doc_type, resolution)

    updates: dict = {}
    for header in _TAX_AMOUNT_HEADERS:
        if header in row:
            # QBS Tax Amount: tax dollars (only SR carries tax; else 0).
            if line.tax_treatment == "SR":
                rate = resolution.jurisdiction.standard_rate or 0.0
                amt = line.gst_amount if line.gst_amount else (net or 0.0) * rate
                updates[header] = round(float(amt or 0.0), 2)
            else:
                updates[header] = 0.0
    for header in _TAX_CODE_HEADERS:
        if header in row:
            if header == "*TaxType":
                updates[header] = tax_code_for
            else:
                updates[header] = line.tax_treatment
    return line.tax_treatment, updates


def _resolve_tax_code(treatment: str, doc_type: str, resolution) -> str:
    """Map a canonical treatment to the target-system tax code for ``resolution``.

    Reads the per-jurisdiction ``code_map`` from the reference YAML. Falls back
    to the SG / QBS mapping when the reference YAML is unavailable, so legacy
    callers see no behaviour change.

    Returns ``""`` (blank) when treatment is None or empty — a None treatment
    means indeterminate/unresolved and must never render as the string "None"
    or silently emit an SR code.
    """
    if not treatment:
        return ""
    from .jurisdiction import _load_reference

    yaml_name = getattr(resolution.jurisdiction, "reference_yaml", None)
    direction = "sales" if doc_type == "sales" else "purchase"
    if yaml_name:
        data = _load_reference(yaml_name) or {}
        code_map = data.get("code_map") or {}
        # Prefer QBS when present (matches the chat tool's expected write format).
        for system in ("qbs", "xero"):
            table = code_map.get(system, {}).get(direction, {})
            if treatment in table:
                return table[treatment]
    # No reference YAML — return the canonical treatment string itself so the
    # caller still gets a meaningful code (was the previous behaviour when
    # only SG was supported).
    return treatment


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
        return None, _empty_ledger_message(tool_context)

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
        working,
        registered=registered,
        tax_keyword=requested_kw,
        state=getattr(tool_context, "state", {}) or {},
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
# Replace-recorded-month write tool (Step 7 / C-3) — gated confirmation
# --------------------------------------------------------------------------- #

#: Month-name / abbreviation → month number for ``replace_recorded_month``.
#: Re-uses the same mapping already defined for ``bank_totals`` (``_MONTHS``).


def _parse_month_arg(month: str, *, fy: str | None = None) -> tuple[int, int]:
    """Parse a flexible ``month`` string into ``(year, month_number)``.

    Accepts:
    - ``"September"`` / ``"Sep"`` / ``"sept"`` (year inferred from *fy*)
    - ``"September 2025"`` / ``"Sep 2025"``
    - ``"2025-09"``
    - ``"09/2025"``

    Args:
        month: The user-supplied month string.
        fy: The loaded FY label (e.g. ``"2026"``).  Used to infer the year when
            the user supplies only a month name with no year.

    Returns:
        ``(year, month_number)`` as ints.

    Raises:
        ValueError: When the month string cannot be parsed or the month number
            is out of range 1–12.
    """
    raw = (month or "").strip()
    if not raw:
        raise ValueError("month must not be empty")

    year_inferred: int | None = None
    month_num: int | None = None

    # "2025-09"
    if "-" in raw and raw.replace("-", "").isdigit():
        parts = raw.split("-")
        if len(parts) == 2 and len(parts[0]) == 4:
            try:
                year_inferred = int(parts[0])
                month_num = int(parts[1])
            except ValueError:
                pass

    # "09/2025"
    if month_num is None and "/" in raw:
        parts = raw.split("/")
        if len(parts) == 2:
            try:
                a, b = int(parts[0]), int(parts[1])
                # "09/2025": first part is month, second is year
                if b > 99:
                    month_num, year_inferred = a, b
                else:
                    month_num, year_inferred = b, a
            except ValueError:
                pass

    # "September 2025" / "Sep 2025"
    if month_num is None:
        tokens = raw.split()
        if len(tokens) == 2:
            name_tok, year_tok = tokens[0], tokens[1]
            mnum = _MONTHS.get(name_tok.lower())
            try:
                yr = int(year_tok)
                if mnum and yr > 0:
                    month_num, year_inferred = mnum, yr
            except ValueError:
                pass
        elif len(tokens) == 1:
            # Pure month name or abbreviation
            mnum = _MONTHS.get(tokens[0].lower())
            if mnum:
                month_num = mnum
            else:
                # Could be a bare number "9" or "09"
                try:
                    month_num = int(tokens[0])
                except ValueError:
                    pass

    if month_num is None:
        raise ValueError(
            f"I couldn't parse {month!r} as a month. "
            "Try formats like \"September\", \"Sep\", \"September 2025\", "
            "\"2025-09\", or \"09/2025\"."
        )

    if not 1 <= month_num <= 12:
        raise ValueError(
            f"Month number {month_num} is out of range (must be 1–12)."
        )

    # Infer year from FY label when the user supplied only a month name.
    if year_inferred is None:
        try:
            year_inferred = int(str(fy).strip()) if fy and str(fy).strip().isdigit() else None
        except (TypeError, ValueError):
            year_inferred = None
        if year_inferred is None:
            from datetime import date as _date
            year_inferred = _date.today().year

    return (year_inferred, month_num)


def replace_recorded_month(tool_context: ToolContext, month: str) -> str:
    """Clear all invoice rows for a month from the FY ledger (gated — asks you to confirm first).

    Use this when you want to re-drop a month's documents (e.g. because you
    uploaded the wrong files) and need the dedup gate to let them through
    again.  The FIRST call counts the rows to be removed and asks for your OK
    — nothing is written.  After you confirm, the month's Purchase and Sales
    rows are cleared and their dedupe keys are purged so re-dropped documents
    will be recorded fresh.

    Only QBS Ledger workbooks are supported.  Bank sheets are unaffected.

    Args:
        tool_context: Injected by ADK; provides session state + confirmation.
        month: The month to clear — flexible format: "September", "Sep",
            "September 2025", "2025-09", "09/2025".

    Returns:
        A short status string.  The commit appends a write spec to
        ``state["pending_ledger_write"]`` for the runner to execute.
    """
    # Software gate — same check as amend/remove.
    software = str(tool_context.state.get("software") or "").strip()
    if software and software not in _SUPPORTED_WRITE_SOFTWARE:
        return (
            f"Editing this ledger layout ({software!r}) from chat isn't supported yet "
            "— only QBS Ledger workbooks can be cleared here."
        )

    # Ledger-loaded gate.
    rows = _get_rows(tool_context)
    if not rows:
        return _empty_ledger_message(tool_context)

    # Parse the month arg.
    fy = str(
        tool_context.state.get("fy_loaded") or tool_context.state.get("fy") or ""
    ).strip() or None
    try:
        year, month_num = _parse_month_arg(month, fy=fy)
    except ValueError as exc:
        return str(exc)

    confirmation = getattr(tool_context, "tool_confirmation", None)

    # ----- Turn 1: count matching rows + request confirmation -----
    if not confirmation:
        # Count matching invoice rows in the in-state ledger snapshot.
        purchase_count = 0
        sales_count = 0
        for row in rows:
            if row.get("_sheet") not in _INVOICE_SHEETS:
                continue
            date_val = row.get("Date")
            if date_val is None:
                continue
            parsed = _parse_row_date(date_val)
            if parsed is None:
                continue
            row_year, row_month = parsed.year, parsed.month
            if row_year == year and row_month == month_num:
                if row.get("_sheet") == "Purchase":
                    purchase_count += 1
                else:
                    sales_count += 1

        total = purchase_count + sales_count
        if total == 0:
            import calendar
            month_name = calendar.month_name[month_num]
            return (
                f"I don't see any invoice rows dated {month_name} {year} in the "
                "loaded ledger — nothing to clear."
            )

        import calendar
        month_name = calendar.month_name[month_num]
        parts: list[str] = []
        if purchase_count:
            parts.append(f"{purchase_count} Purchase")
        if sales_count:
            parts.append(f"{sales_count} Sales")
        rows_desc = " + ".join(parts)

        hint = (
            f"I'll remove {rows_desc} rows dated {month_name} {year} from your ledger "
            f"and clear their dedupe keys so you can re-drop those documents. "
            f"Reply 'yes' to confirm, or 'no' to cancel."
        )
        spec = {"op": "replace_month", "year": year, "month": month_num}
        try:
            tool_context.request_confirmation(hint=hint, payload=spec)
        except Exception:  # noqa: BLE001
            logger.exception(
                "replace_recorded_month: request_confirmation failed "
                "(year=%s month=%s)", year, month_num,
            )
            return "I couldn't open the confirmation step. Please try again."
        return "Awaiting your confirmation before I clear the month."

    # ----- Turn 2: the user answered -----
    if not getattr(confirmation, "confirmed", False):
        return "Okay, I won't clear anything."

    # Re-derive the write spec from the SAME original args (ADR-0009).
    # Re-parse in case state changed between turns (defensive).
    try:
        year2, month_num2 = _parse_month_arg(month, fy=fy)
    except ValueError as exc:
        return str(exc)

    spec = {"op": "replace_month", "year": year2, "month": month_num2}
    pending = tool_context.state.get(PENDING_WRITE_KEY)
    if not isinstance(pending, list):
        pending = []
    pending.append(spec)
    tool_context.state[PENDING_WRITE_KEY] = pending
    return "Confirmed — clearing that month from your ledger now."


# --------------------------------------------------------------------------- #
# Re-extract write tool (Step 7 / ADR-0010) — gated; drains via process_file_event
# --------------------------------------------------------------------------- #


def re_extract_document(tool_context: ToolContext, file_id: str, hints: str) -> str:
    """Re-read a filed document with a hint and replace its ledger rows (gated).

    Use this when the user wants you to re-process an already-filed document with
    a correction — e.g. "re-read the Acme invoice as a credit note" or "re-read
    file F123 and treat the freight line as zero-rated". The corrected read goes
    back through the NORMAL Approve / Edit / Reject card (a human confirms it),
    and its rows replace the old ones (ADR-0010).

    Two-turn confirm (ADR-0009): the FIRST call previews what will happen and
    asks for your OK — nothing runs. After you confirm, a re-extract spec is
    queued for the runner to execute. The ``hints`` text is the whole point of
    the tool, so both ``file_id`` and ``hints`` are required.

    Args:
        tool_context: Injected by ADK; provides session state + confirmation.
        file_id: The Slack file id of the document to re-read (as shown by
            ``list_recent_documents``).
        hints: The free-text instruction steering the re-read (e.g. "read as a
            credit note", "the freight line is zero-rated").

    Returns:
        A short status string. The commit appends a re-extract spec to
        ``state["pending_reextract"]`` for the runner to execute.
    """
    # Software gate — same check as amend/remove/replace_month.
    software = str(tool_context.state.get("software") or "").strip()
    if software and software not in _SUPPORTED_WRITE_SOFTWARE:
        return (
            f"Re-extracting from chat isn't supported for this ledger layout "
            f"({software!r}) yet — only QBS Ledger workbooks can be re-read here."
        )

    file_id = (file_id or "").strip()
    hints = (hints or "").strip()
    if not file_id:
        return (
            "I need the document's file id to re-read it — use "
            "`list_recent_documents` to find it, then tell me which one."
        )
    if not hints:
        return (
            "Tell me HOW to re-read it (the hint is the whole point) — e.g. "
            "\"read it as a credit note\" or \"the freight line is zero-rated\"."
        )

    confirmation = getattr(tool_context, "tool_confirmation", None)

    # ----- Turn 1: preview (honest per ADR-0010) + request confirmation -----
    if not confirmation:
        hint = (
            f"I'll re-read file {file_id} with: '{hints}', then replace its rows "
            "through the normal approval card. This works cleanly when the "
            "document keeps its invoice number (a re-code / tax fix); if the new "
            "read changes the document's identity (e.g. a credit note), I'll add "
            "the corrected version and you may need to clear the old rows with "
            "'clear <month>'. Reply 'yes'."
        )
        spec = {"op": "reextract", "file_id": file_id, "hints": hints}
        try:
            # payload is for audit/UI only; Turn-2 re-derives from the args.
            tool_context.request_confirmation(hint=hint, payload=spec)
        except Exception:  # noqa: BLE001 — never let the gate crash the lane
            logger.exception(
                "re_extract_document: request_confirmation failed (file_id=%s)",
                file_id,
            )
            return "I couldn't open the confirmation step. Please try again."
        return "Awaiting your confirmation before I re-read the document."

    # ----- Turn 2: the user answered -----
    if not getattr(confirmation, "confirmed", False):
        return "Okay, I won't re-read anything."

    # Re-derive the spec from the SAME original args (ADR-0009); never rely on
    # confirmation.payload.
    spec = {"op": "reextract", "file_id": file_id, "hints": hints}
    pending = tool_context.state.get(PENDING_REEXTRACT_KEY)
    if not isinstance(pending, list):
        pending = []
    pending.append(spec)
    tool_context.state[PENDING_REEXTRACT_KEY] = pending
    return "Confirmed — re-reading that document now; I'll send it back through the approval card."


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
{+onboarding_gate?+}
You are the read-only accounting assistant for an SME's FY ledger.
Answer strictly from the data already loaded into your session — never invent
figures, never call external services. The data may be an INVOICE ledger or a
BANK STATEMENT; each tool's docstring tells you which kind it expects.

The client's jurisdiction (region + tax system + currency) is filled in by ADK
from session state via {+region?+} / {+base_currency?+} / {+tax_system?+}
templating — apply the correct tax rules per jurisdiction (Singapore GST vs
Malaysia SST vs cross-border). Never assume Singapore 9% GST for a non-SG
client.

Preamble (filled by ADK from session state — see runner state_delta):
- client: {+client_name+} (UEN {+client_uen?+}, {+region?+}, base {+base_currency?+},
  tax-registered: {+tax_registered?+}, tax system: {+tax_system?+},
  FYE month {+fye_month?+})
- loaded FY: FY{+fy_loaded?+}  ({+ledger_row_count?+} rows)
- Processing history: {+processing_log_count?+} deliveries
- Pending reviews: {+pending_review_count?+} awaiting approval
- Thread-scoped delivery (Phase 3 — only set when the user replies under a
  delivery card; the runner derives these from processing_log + the parent
  message ts of the thread):
  * message_ts: {+thread_delivery_message_ts?+}
  * filenames:   {+thread_delivery_filenames?+}
  * invoice_ids: {+thread_delivery_invoice_ids?+}
  * FY of the delivery: FY{+thread_delivery_fy?+}
  * rows shown in the delivery card table: {+thread_delivery_preview_rows?+}
  * pre-resolved ledger matches (runner prefetch): {+thread_delivery_ledger_matches?+}
  * thread focus from the last account-code answer: {+thread_focus?+}

Routing guidelines:
1. To explain categorization or tax coding decisions (e.g., "why this COA?", "why did you use this account code?", "explain why you used this account code for invoice X"), you MUST NOT use `explain_posted_line`. Instead, you MUST first find the row index by calling `lookup_row` with the invoice ID or filename, and then call `explain_categorization` or `explain_tax_treatment` using that row index. If the user asks about multiple invoices, you MUST alternate: lookup the first invoice, call the explanation tool for it, then lookup the next invoice, call the explanation tool for it, and so on. Do NOT lookup all invoices first.
2. Use `explain_posted_line` ONLY when the user asks about the audit trail, or when they ask for detail combining the posted ledger row, COA, and extraction logs. Do NOT call `explain_posted_line` for "why" categorization/account code questions. For simple account code definitions, use `lookup_coa_account` instead.
3. Write tools (e.g. modify ledger) are gated: propose the change first, and wait for explicit user confirmation before calling the write tool.
4. If the user asks for total purchases, total spend, or expense summaries, call `summarize_by_category`. Do NOT use `pnl_for_fy` unless they specifically ask for overall net profit, total revenue, or a full profit and loss summary.
5. If the ledger is not loaded or has 0 rows, use `diagnose_assistant_context` to check.
6. For processing diagnostics — why a document failed, what was extracted, what's pending review, or processing history — use `get_document_processing_detail`, `list_processing_history`, `list_pending_reviews`, or `diagnose_assistant_context`.
7. To browse source documents in the loaded ledger (by filename, vendor, or date), use `list_recent_documents`. To find a specific ledger row by invoice ID, filename, or vendor name, use `lookup_row`.


For every question, call the single most relevant tool first, then explain the
result in plain English. If a tool reports the data is not loaded, follow the
diagnostic routing above before claiming nothing is there.

CRITICAL — ALWAYS finish your turn with a short plain-English message to the
user summarising the answer in your own words and citing the relevant numbers.
NEVER end your turn with only a tool call and no text reply — the user cannot
see raw tool output, so silence looks like a broken assistant.

Be concise and professional.
""".strip()


def _enrich_instruction_state(state_dict: dict) -> dict:
    """Ensure flat count keys exist for ADK ``{+key?+}`` instruction placeholders.

    The runner injects ``processing_log_count`` / ``pending_review_count`` in
    ``state_delta``; tests may only provide the list keys — derive counts here
    so ``assistant_instruction()`` and ADK runtime injection stay aligned.

    Phase 8 / multi-country: also surface the resolved jurisdiction's tax
    system (``tax_system``) so the instruction template can render the right
    tax-system hint for the chat agent. When the jurisdiction hasn't been
    resolved yet, infer from region: SG -> GST, MY -> SST.
    """
    enriched = dict(state_dict)
    if enriched.get("processing_log_count") is None:
        plog = enriched.get(PROCESSING_LOG_KEY) or enriched.get("processing_log")
        if isinstance(plog, list):
            enriched["processing_log_count"] = len(plog)
    if enriched.get("pending_review_count") is None:
        pending = enriched.get(PENDING_REVIEWS_KEY) or enriched.get("pending_reviews")
        if isinstance(pending, list):
            enriched["pending_review_count"] = len(pending)

    if enriched.get("onboarding_required") and not enriched.get("software"):
        enriched["onboarding_gate"] = (
            "⚠️ This channel is not onboarded yet. The user must run /ledgr settings first. "
            "Tell them this — do NOT attempt to answer ledger questions."
        )

    # Surface the resolved tax system so the chat agent knows whether it is
    # answering GST or SST questions. Read from state if the router has
    # already written it (document lane writes ``tax_system_hint``); otherwise
    # infer from region so the chat prompt is correct on first turn.
    if not enriched.get("tax_system"):
        region = (enriched.get("client_region") or enriched.get("region") or "").strip().upper()
        if region in ("SINGAPORE", "SG", "SGP"):
            enriched["tax_system"] = "GST"
        elif region in ("MALAYSIA", "MY", "MYS", "MSIA"):
            enriched["tax_system"] = "SST"
    return enriched


def assistant_instruction(ctx) -> str:
    """Render ``_BASE_INSTRUCTION`` with ADK's session-state injection.

    Kept for tests and for callers that want a hand-rendered prompt. At
    runtime the agent uses ``_BASE_INSTRUCTION`` directly as a string so
    ADK auto-injects via ``instructions_utils.inject_session_state``. This
    callable reproduces the same behaviour synchronously by walking the
    same template substitution rules (markers: ``{+key+}`` for required,
    ``{+key?+}`` for optional).

    See ``accounting_agents.assistant`` docstring + ADR-0008 for the
    chat-lane state contract.
    """
    try:
        state_obj = ctx.state
        state_dict = _enrich_instruction_state(
            dict(state_obj) if hasattr(state_obj, "items") else {}
        )
    except Exception:  # noqa: BLE001 — never let prompt assembly crash
        return _BASE_INSTRUCTION

    def _sub(match: "re.Match[str]") -> str:  # type: ignore[name-defined]  # noqa: F821
        raw = match.group(0).lstrip("{").rstrip("}").strip().lstrip("+").rstrip("+").strip()
        optional = raw.endswith("?")
        key = raw[:-1] if optional else raw
        val = state_dict.get(key)
        # Treat None and blank strings as missing for optional placeholders
        # (matches ADK's behaviour of collapsing to empty).
        if val is None or (isinstance(val, str) and not val.strip()):
            return "" if optional else match.group(0)
        return str(val)

    try:
        import re as _re

        return _re.sub(r"\{\+[a-zA-Z_][a-zA-Z0-9_]*\?\+\}|\{\+[a-zA-Z_][a-zA-Z0-9_]*\+\}", _sub, _BASE_INSTRUCTION)
    except Exception:  # noqa: BLE001
        return _BASE_INSTRUCTION


def _assistant_before_agent(callback_context: CallbackContext) -> None:
    """Lazy-load and invoke `load_client_profile` to seed client/ledger state for the chat agent."""
    from accounting_agents.agent import load_client_profile
    load_client_profile(callback_context)


def _thread_account_before_model(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """Inject thread-focus preamble when the user asks about a posted account code."""
    try:
        state = callback_context.state
    except Exception:  # noqa: BLE001
        return None

    has_thread = bool(
        state.get("thread_delivery_message_ts")
        or state.get("thread_delivery_ledger_matches")
        or state.get(THREAD_FOCUS_KEY)
    )
    if not has_thread:
        return None

    focus = state.get(THREAD_FOCUS_KEY) or {}
    matches = state.get("thread_delivery_ledger_matches") or []
    inv = ""
    acct = ""
    if isinstance(focus, dict):
        inv = str(focus.get("invoice_id") or "")
        acct = str(focus.get("account_code") or "")
    if not acct and isinstance(matches, list) and matches:
        first = matches[0] if isinstance(matches[0], dict) else {}
        inv = inv or str(first.get("invoice_id") or "")
        acct = acct or str(first.get("account_code") or "")

    if not inv and not acct:
        return None

    preamble = (
        "Thread delivery context is loaded. "
        f"Invoice {inv or '(see thread)'} was posted to account code {acct or '(see ledger matches)'}. "
        "Do NOT ask the user for vendor, line description, or account code. "
        "Call lookup_coa_account or explain_posted_line first, then answer in plain English."
    )
    try:
        llm_request.append_instructions([preamble])
    except Exception:  # noqa: BLE001
        logger.debug("thread account preamble injection failed", exc_info=True)
    return None


def _chat_before_model(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """Thread account/COA preamble injection."""
    return _thread_account_before_model(callback_context, llm_request)


assistant_agent = LlmAgent(
    name="assistant",
    model=config.MODEL_CHAT,
    # P5-slim-instruction: ADK auto-injects session state into string
    # instructions via ``instructions_utils.inject_session_state``. The
    # ``_BASE_INSTRUCTION`` template carries ``{state_key}`` placeholders
    # (with ``{key?}`` for optional ones), so the runner's state_delta fills
    # the preamble at LLM call time — no Python callable needed at runtime.
    instruction=_BASE_INSTRUCTION,
    before_agent_callback=_assistant_before_agent,
    before_model_callback=_chat_before_model,
    tools=[
        bank_totals,
        summarize_by_category,
        pnl_for_fy,
        gst_threshold_check,
        show_client_profile,
        show_learned_mappings,
        model_info,
        explain_categorization,
        lookup_coa_account,
        explain_tax_treatment,
        summarize_recent_activity,
        lookup_row,
        list_recent_documents,
        list_processing_history,
        explain_document_processing,
        get_document_processing_detail,
        explain_posted_line,
        diagnose_assistant_context,
        list_pending_reviews,
        FunctionTool(amend_ledger_row, require_confirmation=True),
        FunctionTool(remove_ledger_row, require_confirmation=True),
        FunctionTool(replace_recorded_month, require_confirmation=True),
        FunctionTool(re_extract_document, require_confirmation=True),
        learn_mapping,
    ],
)

__all__ = [
    "assistant_agent",
    "assistant_instruction",
    "LEDGER_DATA_KEY",
    "PENDING_WRITE_KEY",
    "PENDING_LEARN_KEY",
    "PENDING_REEXTRACT_KEY",
    "PROCESSING_LOG_KEY",
    "GST_THRESHOLD_SGD",
    "amend_ledger_row",
    "remove_ledger_row",
    "replace_recorded_month",
    "re_extract_document",
    "learn_mapping",
    "bank_totals",
    "summarize_by_category",
    "pnl_for_fy",
    "gst_threshold_check",
    "show_client_profile",
    "show_learned_mappings",
    "model_info",
    "explain_categorization",
    "lookup_coa_account",
    "explain_tax_treatment",
    "summarize_recent_activity",
    "lookup_row",
    "list_recent_documents",
    "list_processing_history",
    "explain_document_processing",
    "get_document_processing_detail",
    "explain_posted_line",
    "diagnose_assistant_context",
    "list_pending_reviews",
]
