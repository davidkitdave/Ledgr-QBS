"""Gated write tools for the chat assistant."""

from __future__ import annotations

import logging

from google.adk.tools import ToolContext

from ..constants import (
    PENDING_LEARN_KEY,
    PENDING_REEXTRACT_KEY,
    PENDING_WRITE_KEY,
    _EDITABLE_FIELD_HEADERS,
    _INVOICE_SHEETS,
    _SUPPORTED_WRITE_SOFTWARE,
    _TAX_FIELD_ALIASES,
)
from ._helpers import (
    _MONTHS,
    _build_amend_spec,
    _build_remove_spec,
    _empty_ledger_message,
    _get_rows,
    _load_target_row,
    _parse_row_date,
)

logger = logging.getLogger(__name__)

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
