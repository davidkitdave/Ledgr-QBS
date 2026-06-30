"""Shared row/state helpers for assistant tools."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime

from google.adk.tools import ToolContext

from accounting_agents.jurisdiction import (
    REGION_MALAYSIA,
    REGION_SINGAPORE,
    _norm_region,
    _resolve_client_currency,
    registration_threshold_for_region,
    resolve_jurisdiction,
    write_to_state,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice

from accounting_agents.tax_reasoning import reason_one_invoice as _reason_one_invoice

from ..constants import (
    LEDGER_DATA_KEY,
    PENDING_REVIEWS_KEY,
    PROCESSING_LOG_KEY,
    _EDITABLE_FIELD_HEADERS,
    _INVOICE_SHEETS,
    _SIGNATURE_COLS,
    _SUPPORTED_WRITE_SOFTWARE,
    _TAX_AMOUNT_HEADERS,
    _TAX_CODE_HEADERS,
    _TAX_FIELD_ALIASES,
)

logger = logging.getLogger(__name__)

#: Month-name / abbreviation → month number, for bank period filtering.
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

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
    (legacy). When region is missing or unsupported, returns ``(0, "", "")`` so
    callers fail loud — never silently default to SG (C10).

    Order of precedence:
    1. Env override ``LEDGR_TAX_REGISTRATION_THRESHOLD_<REGION>`` (most explicit).
    2. Per-region ``registration_threshold`` from the jurisdiction YAML.
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
    return 0.0, "", ""


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
    # Date — QBS uses "Invoice Date"; Xero uses "*InvoiceDate".
    if not out.get("Date") and not out.get("date"):
        d = out.get("Invoice Date") or out.get("*InvoiceDate")
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


def filename_matches_query(needle: str, stored: str) -> bool:
    """Return True when ``needle`` identifies ``stored`` (full or partial)."""
    n = (needle or "").strip().lower()
    s = (stored or "").strip().lower()
    if not n or not s:
        return False
    if n == s or n in s or s in n:
        return True
    n_bare = n[5:] if n.startswith("xero:") else n
    s_bare = s[5:] if s.startswith("xero:") else s
    if n_bare and (n_bare == s_bare or n_bare in s_bare or s_bare in n_bare):
        return True
    return False


def row_search_text(row: dict) -> str:
    """Concatenate ledger columns that ``lookup_row`` should search."""
    cols = (
        "Description",
        "description",
        "Vendor",
        "vendor",
        "Reference",
        "Source Filename",
        "source_filename",
        "*InvoiceNumber",
        "*ContactName",
        "*Description",
        "Account Code / COA",
        "account_code",
        "category",
    )
    return " ".join(str(row.get(col) or "") for col in cols).lower()


def _normalize_coa_code(code: str) -> str:
    return (code or "").strip().lower().replace(" ", "")


def find_coa_by_code(state: dict, account_code: str) -> dict | None:
    """Return the COA dict for ``account_code`` (exact then normalized match)."""
    needle = (account_code or "").strip()
    if not needle:
        return None
    coa_list = state.get("coa") or []
    if not isinstance(coa_list, list):
        return None
    needle_norm = _normalize_coa_code(needle)
    fuzzy: dict | None = None
    for entry in coa_list:
        if not isinstance(entry, dict):
            continue
        ec = str(entry.get("code") or "").strip()
        if not ec:
            continue
        if ec == needle or ec.lower() == needle.lower():
            return entry
        if _normalize_coa_code(ec) == needle_norm:
            fuzzy = entry
    return fuzzy


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
    from accounting_agents.jurisdiction import _load_reference

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
