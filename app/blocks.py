"""Pure Block Kit builders — return dicts/lists, no Slack API calls."""

from __future__ import annotations

import urllib.parse

from app.native_blocks_compat import supports_native_blocks
from accounting_agents.jurisdiction import supported_regions
from invoice_processing.export.exporters import (
    SLACK_DATA_TABLE_MAX_COLS,
    PreviewColumn,
    load_erp_profile_for_system,
    normalize_software_preview_key,
    preview_columns_from_profile,
    software_label,
)


def _enc(val: str | None) -> str:
    """%encode a value (used in Slack action ``value`` payloads)."""
    return urllib.parse.quote(str(val or ""), safe="") or "-"


_XERO_PURCHASE_COLS: list[PreviewColumn] = [
    PreviewColumn("Contact",      "*ContactName",   "raw_text"),
    PreviewColumn("Invoice #",    "*InvoiceNumber", "raw_text"),
    PreviewColumn("Invoice Date", "*InvoiceDate",   "raw_text"),
    PreviewColumn("Description",  "Description",    "raw_text"),
    PreviewColumn("Account",      "*AccountCode",   "raw_text"),
    PreviewColumn("Tax Type",     "*TaxType",       "raw_text"),
    PreviewColumn("Unit Amount",  "*UnitAmount",    "raw_number"),
    PreviewColumn("Total",        "Total",          "raw_number"),
    PreviewColumn("Currency",     "Currency",       "raw_text"),
]

_XERO_SALES_COLS: list[PreviewColumn] = [
    PreviewColumn("Contact",      "*ContactName",   "raw_text"),
    PreviewColumn("Invoice #",    "*InvoiceNumber", "raw_text"),
    PreviewColumn("Invoice Date", "*InvoiceDate",   "raw_text"),
    PreviewColumn("Description",  "*Description",   "raw_text"),
    PreviewColumn("Account",      "*AccountCode",   "raw_text"),
    PreviewColumn("Tax Type",     "*TaxType",       "raw_text"),
    PreviewColumn("Unit Amount",  "*UnitAmount",    "raw_number"),
    PreviewColumn("Total",        "Total",          "raw_number"),
    PreviewColumn("Currency",     "Currency",       "raw_text"),
]

_QBS_PURCHASE_COLS: list[PreviewColumn] = [
    PreviewColumn("Invoice Date",  "Invoice Date",       "raw_text"),
    PreviewColumn("Invoice #",     "Invoice Number",     "raw_text"),
    PreviewColumn("Vendor",        "Vendor Name",        "raw_text"),
    PreviewColumn("Description",   "Description",        "raw_text"),
    PreviewColumn("Account / COA", "Account Code / COA", "raw_text"),
    PreviewColumn("Sub Total",     "Sub Total",          "raw_number"),
    PreviewColumn("Tax Amount",    "Tax Amount",         "raw_number"),
    PreviewColumn("Total Amount",  "Total Amount",       "raw_number"),
    PreviewColumn("Currency",      "Currency",           "raw_text"),
]

_QBS_SALES_COLS: list[PreviewColumn] = [
    PreviewColumn("Invoice Date",  "Invoice Date",       "raw_text"),
    PreviewColumn("Invoice #",     "Invoice Number",     "raw_text"),
    PreviewColumn("Customer",      "Customer Name",      "raw_text"),
    PreviewColumn("Description",   "Description",        "raw_text"),
    PreviewColumn("Account / COA", "Account Code / COA", "raw_text"),
    PreviewColumn("Amount",        "Amount",             "raw_number"),
    PreviewColumn("Tax Amount",    "Tax Amount",         "raw_number"),
    PreviewColumn("Total",         "Total",              "raw_number"),
    PreviewColumn("Currency",      "Currency",           "raw_text"),
]

_BANK_COLS: list[PreviewColumn] = [
    PreviewColumn("Date",        "Date",        "raw_text"),
    PreviewColumn("Description", "Description", "raw_text"),
    PreviewColumn("Withdrawal",  "Withdrawal",  "raw_number"),
    PreviewColumn("Deposit",     "Deposit",     "raw_number"),
    PreviewColumn("Balance",     "Balance",     "raw_number"),
    PreviewColumn("Currency",    "Currency",    "raw_text"),
]

def preview_column_spec(*, software: str, sheet: str) -> list[PreviewColumn]:
    """Return the curated preview columns for a (software, sheet) combination.

    Bank sheets (anything other than ``"Purchase"`` or ``"Sales"``) always
    use the 6-col bank spec regardless of software.  Unresolved software
    returns an empty spec (caller should flag for review).
    """
    if sheet not in ("Purchase", "Sales"):
        return _BANK_COLS
    norm = normalize_software_preview_key(software)
    if norm is None:
        return []
    if norm == "xero":
        return _XERO_PURCHASE_COLS if sheet == "Purchase" else _XERO_SALES_COLS
    if norm in ("autocount", "sql_account"):
        profile = load_erp_profile_for_system(norm)
        if profile is not None:
            return preview_columns_from_profile(profile, sheet)
        return []
    # qbs_ledger (default)
    return _QBS_PURCHASE_COLS if sheet == "Purchase" else _QBS_SALES_COLS

# Ordered pipeline stage keys and their display titles (ADR-0011 layers).
PIPELINE_STAGES: tuple[str, ...] = (
    "understand",
    "policy",
    "commit",
)

_STAGE_TITLES: dict[str, str] = {
    "understand": "Understanding document",
    "policy": "Applying your rules",
    "commit": "Ready to file",
}

_STATUS_MARKER: dict[str, str] = {
    "complete": ":white_check_mark:",
    "in_progress": ":large_blue_circle:",
    "pending": ":white_circle:",
    "failed": ":x:",
}


def _rich_text_from_string(text: str) -> dict:
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [{"type": "text", "text": text}],
            }
        ],
    }


def processing_plan_blocks(
    file_label: str,
    *,
    stages: list[dict],
    channel_id: str | None = None,
) -> list[dict]:
    """Build a live processing-status block for a document run.

    When supports_native_blocks is True, emits a single ``plan`` block with
    per-task status cards. Falls back to section+context with emoji markers.

    Args:
        file_label: Human label for the file being processed (e.g. filename).
        stages: Ordered list of stage dicts with keys:
            task_id, title, status (pending|in_progress|complete|failed),
            output (str or None).
        channel_id: Used by supports_native_blocks() for per-channel probe.
    """
    if supports_native_blocks(channel_id):
        tasks = []
        for s in stages:
            status = s["status"]
            title = s["title"]
            if status == "failed":
                status = "complete"
                title = f":x: {title}"
            task: dict = {
                "task_id": s["task_id"],
                "title": title,
                "status": status,
            }
            if s.get("output") is not None:
                task["output"] = _rich_text_from_string(s["output"])
            tasks.append(task)
        return [
            {
                "type": "plan",
                "title": f"Processing {file_label}",
                "tasks": tasks,
            }
        ]

    # Fallback: section header + context line with emoji markers per stage.
    markers = []
    for s in stages:
        marker = _STATUS_MARKER.get(s["status"], ":white_circle:")
        markers.append(f"{marker} {s['title']}")
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Processing {file_label}*",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": " · ".join(markers)},
            ],
        },
    ]


def batch_overall_progress_line(
    *,
    total: int,
    done: int,
    doc_rows: list[dict],
) -> str:
    """Human overall line — active work is not '0 processed'."""
    complete = sum(1 for r in doc_rows if r.get("status") == "complete")
    failed = sum(1 for r in doc_rows if r.get("status") == "failed")
    in_progress = sum(1 for r in doc_rows if r.get("status") == "in_progress")
    if complete >= total:
        return f"{complete} of {total} complete"
    parts: list[str] = []
    if in_progress:
        parts.append(f"{in_progress} in progress")
    if complete:
        parts.append(f"{complete} complete")
    if failed:
        parts.append(f"{failed} failed")
    queued = max(0, total - complete - failed - in_progress)
    if queued and not in_progress:
        parts.append(f"{queued} queued")
    return " · ".join(parts) if parts else f"{done}/{total} done"


def _batch_progress_expanded_blocks(
    *,
    headline: str,
    total: int,
    done: int,
    doc_rows: list[dict],
) -> list[dict]:
    """Always-visible batch progress (section + context).

    Slack native ``plan`` blocks collapse back to closed on every
    ``chat.update`` — there is no server-side ``expanded`` flag. For batch
    follow-along UX we use section+context so every doc line stays visible
    while the coordinator edits the placeholder message in place.
    """
    overall = batch_overall_progress_line(total=total, done=done, doc_rows=doc_rows)
    lines: list[str] = [f"*{headline}*", f"_Overall — {overall}_"]
    for i, row in enumerate(doc_rows, start=1):
        marker = _STATUS_MARKER.get(row.get("status") or "in_progress", ":white_circle:")
        stage = row.get("stage") or "queued"
        label = row.get("file_label") or f"doc {i}"
        line = f"{marker} doc {i}/{total} — `{label}` — {stage}"
        if row.get("detail"):
            line += f" · {row['detail']}"
        lines.append(line)
    body = "\n".join(lines)
    # Slack section mrkdwn limit is 3000 chars; truncate safely for huge batches.
    if len(body) > 2900:
        body = body[:2900] + "\n…"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
            "expand": True,
        },
    ]


def processing_plan_headline(*, total: int, title: str | None = None) -> str:
    """Human headline for the live processing plan block.

    Single-file drops say "Processing document" — "batch" implies multiple
    uploads and reads oddly when ``total == 1``.
    """
    if title:
        return title
    if total == 1:
        return "Processing document"
    return f"Processing batch ({total} documents)"


def batch_processing_plan_blocks(
    *,
    total: int,
    done: int,
    doc_rows: list[dict],
    channel_id: str | None = None,
    title: str | None = None,
) -> list:
    """Build the batch-level thinking plan (one task per document).

    A single top-level Block Kit ``plan`` block listing every document in the
    batch as a task, with the current per-doc stage as the task ``output``. This
    is what the batch coordinator ``chat_update``s on the placeholder message
    while the batch runs — one top-level message, with all thinking visible
    inline, no per-doc accordion stampede.

    Each ``doc_row`` is a dict with:
      - ``file_label``: short human label (e.g. filename)
      - ``stage``: one of ``"queued"``, ``"understanding"``, ``"applying_rules"``,
        ``"awaiting_review"``, ``"ready"``, ``"rejected"``, ``"duplicate"``
      - ``detail``: short output line for the current stage (e.g. the
        vendor / total / lines summary from the Understand call)
      - ``status``: ``"in_progress" | "complete" | "failed"``

    Falls back to a section + context block list when the channel doesn't
    support native ``plan`` blocks.

    **Batch UX:** Uses the native ``plan`` block (agent thinking pattern) when the
    channel supports it. Slack may collapse the chevron on each ``chat.update`` —
    that is a platform limitation. Set ``LEDGR_BATCH_EXPANDED_PROGRESS=1`` to opt
    into always-visible section text instead.
    """
    import os

    headline = processing_plan_headline(total=total, title=title)
    use_expanded = os.environ.get("LEDGR_BATCH_EXPANDED_PROGRESS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if use_expanded or not supports_native_blocks(channel_id):
        return _batch_progress_expanded_blocks(
            headline=headline,
            total=total,
            done=done,
            doc_rows=doc_rows,
        )
    if supports_native_blocks(channel_id):
        tasks = []
        for i, row in enumerate(doc_rows, start=1):
            status = row.get("status") or "in_progress"
            stage = row.get("stage") or "queued"
            label = row.get("file_label") or f"doc {i}"
            # Plan block task titles are plain text — Slack emoji shortcodes do not render.
            title_line = f"doc {i}/{total} — {label}"
            task: dict = {
                "task_id": f"doc_{i}",
                "title": title_line,
                "status": (
                    "complete"
                    if status == "complete"
                    else ("failed" if status == "failed" else "in_progress")
                ),
            }
            detail_bits: list[str] = [stage]
            if row.get("detail"):
                detail_bits.append(str(row["detail"]))
            if detail_bits:
                task["output"] = _rich_text_from_string(" · ".join(detail_bits))
            tasks.append(task)
        # Add an "overall" task that summarizes how many docs have finished —
        # helpful at-a-glance state for the user looking at the plan block.
        overall_status = "complete" if done >= total else "in_progress"
        overall_line = batch_overall_progress_line(total=total, done=done, doc_rows=doc_rows)
        overall_task: dict = {
            "task_id": "overall",
            "title": f"Overall — {overall_line}",
            "status": overall_status,
            "output": _rich_text_from_string(overall_line),
        }
        return [
            {
                "type": "plan",
                "title": headline,
                "tasks": [
                    overall_task,
                    *tasks,
                ],
            }
        ]
    # Fallback: section header + per-doc line (non-native channels).
    return _batch_progress_expanded_blocks(
        headline=headline,
        total=total,
        done=done,
        doc_rows=doc_rows,
    )


_MONTHS = [
    (1, "January"), (2, "February"), (3, "March"), (4, "April"),
    (5, "May"), (6, "June"), (7, "July"), (8, "August"),
    (9, "September"), (10, "October"), (11, "November"), (12, "December"),
]

_SOFTWARE_OPTIONS = ["QBS Ledger", "Xero", "AutoCount", "SQL Account"]


def welcome_blocks() -> list:
    """Welcome card posted when the bot joins a channel."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Welcome to Ledgr!* :ledger:\n"
                    "I'm your accounting document assistant. "
                    "Set up this client to get started."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Set up this client", "emoji": True},
                    "action_id": "ledgr_setup_open",
                    "style": "primary",
                }
            ],
        },
    ]


def onboarding_modal(prefill: dict | None = None) -> dict:
    """Build the onboarding modal view dict.

    Args:
        prefill: optional dict with keys client_name, region, fye_month,
                 accounting_software, gst_registered (bool) to pre-populate the
                 modal for /ledgr settings.
    """
    p = prefill or {}

    # --- block 1: client_name ---
    client_name_block = {
        "type": "input",
        "block_id": "client_name",
        "label": {"type": "plain_text", "text": "Client company name"},
        "element": {
            "type": "plain_text_input",
            "action_id": "val",
            **({"initial_value": p["client_name"]} if p.get("client_name") else {}),
        },
    }

    # --- block 2: region ---
    region_initial = None
    if p.get("region") in supported_regions():
        region_val = p["region"]
        region_initial = {
            "text": {"type": "plain_text", "text": region_val.title()},
            "value": region_val,
        }

    region_block = {
        "type": "input",
        "block_id": "region",
        "label": {"type": "plain_text", "text": "Client tax region"},
        "element": {
            "type": "static_select",
            "action_id": "val",
            "options": [
                {"text": {"type": "plain_text", "text": code.title()}, "value": code}
                for code in supported_regions()
            ],
            **({"initial_option": region_initial} if region_initial else {}),
        },
    }

    # --- block 3: fye_month ---
    fye_initial = None
    if p.get("fye_month") is not None:
        month_num = int(p["fye_month"])
        label = next((name for num, name in _MONTHS if num == month_num), None)
        if label:
            fye_initial = {"text": {"type": "plain_text", "text": label}, "value": str(month_num)}

    fye_block = {
        "type": "input",
        "block_id": "fye_month",
        "label": {"type": "plain_text", "text": "Financial year-end month"},
        "element": {
            "type": "static_select",
            "action_id": "val",
            "options": [
                {"text": {"type": "plain_text", "text": name}, "value": str(num)}
                for num, name in _MONTHS
            ],
            **({"initial_option": fye_initial} if fye_initial else {}),
        },
    }

    # --- block 4: accounting_software ---
    sw_initial = None
    if p.get("accounting_software") in _SOFTWARE_OPTIONS:
        sw_val = p["accounting_software"]
        sw_initial = {"text": {"type": "plain_text", "text": sw_val}, "value": sw_val}

    software_block = {
        "type": "input",
        "block_id": "accounting_software",
        "label": {"type": "plain_text", "text": "Accounting software"},
        "element": {
            "type": "static_select",
            "action_id": "val",
            "options": [
                {"text": {"type": "plain_text", "text": sw}, "value": sw}
                for sw in _SOFTWARE_OPTIONS
            ],
            **({"initial_option": sw_initial} if sw_initial else {}),
        },
    }

    # --- block 5: gst_registered ---
    gst_registered = p.get("gst_registered")
    gst_initial = None
    if gst_registered is True:
        gst_initial = {"text": {"type": "plain_text", "text": "Yes"}, "value": "yes"}
    elif gst_registered is False:
        gst_initial = {"text": {"type": "plain_text", "text": "No"}, "value": "no"}

    gst_block = {
        "type": "input",
        "block_id": "gst_registered",
        "label": {"type": "plain_text", "text": "GST registered?"},
        "element": {
            "type": "radio_buttons",
            "action_id": "val",
            "options": [
                {"text": {"type": "plain_text", "text": "Yes"}, "value": "yes"},
                {"text": {"type": "plain_text", "text": "No"}, "value": "no"},
            ],
            **({"initial_option": gst_initial} if gst_initial is not None else {}),
        },
    }

    return {
        "type": "modal",
        "callback_id": "ledgr_onboarding",
        "title": {"type": "plain_text", "text": "Set up client"},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": [client_name_block, region_block, fye_block, software_block, gst_block],
    }


def processing_ack_blocks(n_files: int) -> list:
    """Instant 'I got your file(s)' card posted the moment a document is dropped, so the
    user can see the bot is working before the (~30s) pipeline finishes."""
    plural = "s" if n_files != 1 else ""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":inbox_tray: *Got it* — processing *{n_files}* document{plural} now. "
                    "This usually takes ~30s; I'll post the ledger here when it's done."
                ),
            },
        }
    ]


def needs_setup_blocks() -> list:
    """Message posted when a file is shared to a channel that has no client profile."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*This channel isn't set up yet.*\n"
                    "Tap *Set up this client* to configure it before sending documents."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Set up this client", "emoji": True},
                    "action_id": "ledgr_setup_open",
                    "style": "primary",
                }
            ],
        },
    ]


def _fmt_money(amount: float | None, currency: str = "") -> str:
    """Format a document total with its currency, e.g. 'SGD 1,234.50'."""
    if amount is None:
        return "—"
    cur = (currency or "?").strip().upper()
    return f"{cur} {amount:,.2f}"


_MAX_SECTION = 2900  # Slack section text hard-fails > 3000 chars; keep headroom.


def _clamp_section(text: str) -> str:
    """Keep a Block Kit section's mrkdwn text under Slack's 3000-char ceiling."""
    return text if len(text) <= _MAX_SECTION else text[: _MAX_SECTION - 1] + "…"


def _per_doc_line(doc) -> str:
    """Build one concise mrkdwn line for a processed document (item: rich per-doc card).

    Invoices / receipts show counterparty, invoice number, date, total, and the
    FY + workbook they landed in. Bank statements show a simpler bank/period/N-txns
    line (they carry ``doc.bank``, not ``doc.normalized``). A ``reconciled is False``
    doc is prefixed with a ``:warning: needs review`` marker plus the reason.
    """
    route = getattr(doc, "route", None)
    fy = getattr(route, "fy", None)
    workbook = getattr(route, "workbook", None)
    dest = ""
    if fy is not None and workbook:
        dest = f"\n   ↳ FY{fy} · `{workbook}`"
    elif workbook:
        dest = f"\n   ↳ `{workbook}`"

    needs_review = getattr(doc, "reconciled", True) is False
    marker = ""
    if needs_review:
        note = (getattr(doc, "note", "") or "").strip()
        # Keep the reason short — an error note can be an unbounded exception string.
        reason = note if len(note) <= 140 else note[:139] + "…"
        if note.upper().startswith("ERROR"):
            marker = f":x: *failed to process*{f' — {reason}' if reason else ''}\n"
        else:
            marker = f":warning: *needs review*{f' — {reason}' if reason else ''}\n"

    if getattr(doc, "doc_type", None) in ("bank_statement", "bank") or getattr(doc, "bank", None) is not None:
        bank = getattr(doc, "bank", None)
        accounts = getattr(bank, "accounts", None) or []
        names = ", ".join(a.bank_name for a in accounts if getattr(a, "bank_name", None)) or "Bank statement"
        period = next((a.statement_period for a in accounts if getattr(a, "statement_period", None)), None)
        n_txns = sum(len(getattr(a, "transactions", []) or []) for a in accounts)
        parts = [f"*{names}*"]
        if period:
            parts.append(period)
        parts.append(f"{n_txns} transaction{'s' if n_txns != 1 else ''}")
        return _clamp_section(f"{marker}:bank: " + "  •  ".join(parts) + dest)

    norm = getattr(doc, "normalized", None)
    direction = (getattr(doc, "direction", None) or "").strip().lower()
    party = None
    if norm is not None:
        party = norm.customer if direction == "sales" else norm.supplier
    counterparty = (getattr(party, "name", None) or "Unknown").strip() or "Unknown"
    inv_no = getattr(norm, "invoice_number", None)
    inv_date = getattr(norm, "invoice_date", None)
    total = getattr(norm, "doc_total", None)
    currency = (getattr(norm, "currency", None) or "").strip().upper() or "?"

    parts = [f"*{counterparty}*"]
    if inv_no:
        parts.append(f"#{inv_no}")
    if inv_date is not None:
        parts.append(inv_date.isoformat() if hasattr(inv_date, "isoformat") else str(inv_date))
    parts.append(_fmt_money(total, currency))
    return _clamp_section(f"{marker}:page_facing_up: " + "  •  ".join(parts) + dest)


_MAX_CARD_TITLE = 150
_MAX_CARD_BODY = 500
# Slack native ``card`` block body/subtitle hard limit (API rejects at 201).
_MAX_NATIVE_CARD_BODY = 200


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def per_doc_card(
    doc,
    *,
    actions: list[str] | None = None,
    op_id: str | None = None,
    channel_id: str | None = None,
) -> list[dict]:
    """Build a per-document block.

    Native path: a ``card`` block with title/subtitle/body/actions.
    Fallback path: the existing mrkdwn section (+ optional actions block).
    """
    actions = actions or []

    if supports_native_blocks(channel_id):
        return _per_doc_card_native(doc, actions=actions, op_id=op_id)
    return _per_doc_card_fallback(doc, actions=actions, op_id=op_id)


def _doc_get(doc, *keys, default=None):
    """Retrieve a value from doc, supporting both attribute-style objects and plain dicts."""
    for key in keys:
        val = doc.get(key) if isinstance(doc, dict) else getattr(doc, key, None)
        if val is not None:
            return val
    return default


def _per_doc_card_native(doc, *, actions: list[str], op_id: str | None) -> list[dict]:
    doc_type = _doc_get(doc, "doc_type")
    is_bank = (
        doc_type in ("bank_statement", "bank")
        or _doc_get(doc, "bank") is not None
    )

    if is_bank:
        bank = _doc_get(doc, "bank")
        if isinstance(bank, dict):
            # plain-dict bank shape: keys bank_name, period, txn_count
            title_raw = bank.get("bank_name") or "Bank statement"
            subtitle_raw = bank.get("period") or ""
            n_txns = bank.get("txn_count") or bank.get("account_number") and 0 or 0
            body_raw = f"{n_txns} transaction{'s' if n_txns != 1 else ''}"
        else:
            accounts = getattr(bank, "accounts", None) or []
            title_raw = (
                ", ".join(a.bank_name for a in accounts if getattr(a, "bank_name", None))
                or "Bank statement"
            )
            period = next(
                (a.statement_period for a in accounts if getattr(a, "statement_period", None)),
                None,
            )
            n_txns = sum(len(getattr(a, "transactions", []) or []) for a in accounts)
            subtitle_raw = period or ""
            body_raw = f"{n_txns} transaction{'s' if n_txns != 1 else ''}"
    else:
        # Try plain-dict doc shape first (keys: counterparty, invoice_number, etc.)
        counterparty = _doc_get(doc, "counterparty")
        norm = None
        direction = (_doc_get(doc, "direction") or "").strip().lower()
        if counterparty is not None:
            title_raw = counterparty.strip() or "Unknown"
            inv_no = _doc_get(doc, "invoice_number")
            inv_date = _doc_get(doc, "invoice_date")
            date_str = str(inv_date) if inv_date else ""
            currency = (_doc_get(doc, "currency") or "").strip().upper() or "?"
            total = _doc_get(doc, "total")
            tax_code = _doc_get(doc, "tax_code")
            account_code = _doc_get(doc, "account_code")
        else:
            # Object-style ProcessedDoc
            norm = _doc_get(doc, "normalized")
            if not direction:
                direction = (_doc_get(doc, "direction") or "").strip().lower()
            party = None
            if norm is not None:
                party = norm.customer if direction == "sales" else norm.supplier
            title_raw = (getattr(party, "name", None) or "Unknown").strip() or "Unknown"
            inv_no = getattr(norm, "invoice_number", None)
            inv_date = getattr(norm, "invoice_date", None)
            date_str = (
                inv_date.isoformat() if inv_date and hasattr(inv_date, "isoformat") else str(inv_date)
            ) if inv_date else ""
            currency = (getattr(norm, "currency", None) or "").strip().upper() or "?"
            total = getattr(norm, "doc_total", None)
            tax_code = None
            account_code = None

        subtitle_parts = []
        direction_reason = _doc_get(doc, "direction_reason")
        if direction_reason is None and norm is not None:
            direction_reason = getattr(norm, "direction_reason", None)
        if not direction and norm is not None:
            direction = (getattr(norm, "doc_type", None) or "").strip().lower()
        if direction in ("purchase", "sales"):
            sheet = "Purchase" if direction == "purchase" else "Sales"
            subtitle_parts.append(sheet)
        if inv_no:
            subtitle_parts.append(f"Invoice #{inv_no}")
        if date_str:
            subtitle_parts.append(date_str)
        if direction_reason:
            subtitle_parts.append(direction_reason)
        subtitle_raw = " · ".join(subtitle_parts)

        body_parts = [_fmt_money(total, currency)]
        if tax_code:
            body_parts.append(tax_code)
        if account_code:
            body_parts.append(str(account_code))
        body_raw = " · ".join(p for p in body_parts if p and p != "—")

    # Route / FY / workbook suffix — works for both plain dicts and objects.
    fy = _doc_get(doc, "fy")
    workbook = _doc_get(doc, "workbook_name")
    if fy is None or workbook is None:
        route = _doc_get(doc, "route")
        if route is not None:
            fy = fy if fy is not None else getattr(route, "fy", None)
            workbook = workbook if workbook is not None else getattr(route, "workbook", None)

    body_suffix_parts = []
    if fy is not None:
        body_suffix_parts.append(f"FY{fy}")
    if workbook:
        body_suffix_parts.append(workbook)
    if body_suffix_parts:
        suffix = " / ".join(body_suffix_parts)
        full_body = f"{body_raw} · {suffix}" if body_raw else suffix
    else:
        full_body = body_raw

    # Bug 1 fix: title/subtitle/body must be mrkdwn text objects, not bare strings.
    card: dict = {
        "type": "card",
        "title": {"type": "mrkdwn", "text": _truncate(title_raw, _MAX_CARD_TITLE)},
    }
    if subtitle_raw:
        card["subtitle"] = {"type": "mrkdwn", "text": _truncate(subtitle_raw, _MAX_CARD_TITLE)}
    if full_body:
        card["body"] = {"type": "mrkdwn", "text": _truncate(full_body, _MAX_NATIVE_CARD_BODY)}

    # reconciled check: plain dicts use key "reconciled", objects use attribute.
    reconciled = _doc_get(doc, "reconciled")
    needs_review = reconciled is False
    if needs_review:
        note = (_doc_get(doc, "note") or "").strip()
        if note.upper().startswith("ERROR"):
            label = "failed to process"
        else:
            label = "needs review"
        reason = note if len(note) <= _MAX_NATIVE_CARD_BODY else note[: _MAX_NATIVE_CARD_BODY - 1] + "…"
        subtext = f"{label} — {reason}" if reason else label
        subtext_val = subtext[: _MAX_NATIVE_CARD_BODY] if len(subtext) > _MAX_NATIVE_CARD_BODY else subtext
        # Bug 1 fix: subtext must also be a mrkdwn text object.
        card["subtext"] = {"type": "mrkdwn", "text": subtext_val}

    if actions:
        # Bug 2 fix: resolve file_id from plain dict keys first, then object attrs.
        file_id = (
            _doc_get(doc, "file_id")
            or _doc_get(doc, "doc_key")
            or _doc_get(doc, "doc_id")
            or (getattr(doc, "path", None) if not isinstance(doc, dict) else None)
        )

        def _btn(action_id: str, label: str, value: str | None) -> dict | None:
            """Return a button dict, or None if value is empty (Bug 3 fix)."""
            resolved = op_id if action_id == "ledgr_per_doc_edit" else value
            resolved = resolved or op_id or file_id
            if not resolved:
                import warnings
                warnings.warn(
                    f"per_doc_card: omitting {action_id!r} button — no usable value",
                    stacklevel=4,
                )
                return None
            return {
                "type": "button",
                "text": {"type": "plain_text", "text": label},
                "action_id": action_id,
                "value": resolved,
            }

        button_builders = {
            "reextract": lambda: _btn("ledgr_per_doc_reextract", "Re-extract", file_id),
            "edit":      lambda: _btn("ledgr_per_doc_edit",      "Edit",        op_id or file_id),
            "view_row":  lambda: _btn("ledgr_per_doc_view_row",  "View row",    file_id),
        }
        built = [button_builders[a]() for a in actions if a in button_builders]
        non_empty = [b for b in built if b is not None]
        if non_empty:
            card["actions"] = non_empty

    return [card]


def _per_doc_card_fallback(doc, *, actions: list[str], op_id: str | None) -> list[dict]:
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": _per_doc_line(doc)}}
    ]
    if actions:
        file_id = getattr(doc, "file_id", None) or getattr(doc, "path", None) or ""
        button_defs = {
            "reextract": {
                "type": "button",
                "text": {"type": "plain_text", "text": "Re-extract"},
                "action_id": "ledgr_per_doc_reextract",
                "value": file_id,
            },
            "edit": {
                "type": "button",
                "text": {"type": "plain_text", "text": "Edit"},
                "action_id": "ledgr_per_doc_edit",
                "value": op_id or file_id or "",
            },
            "view_row": {
                "type": "button",
                "text": {"type": "plain_text", "text": "View row"},
                "action_id": "ledgr_per_doc_view_row",
                "value": file_id,
            },
        }
        blocks.append(
            {"type": "actions", "elements": [button_defs[a] for a in actions if a in button_defs]}
        )
    return blocks


def result_card(
    *,
    n_files: int,
    n_processed: int,
    workbooks: list[str],
    errors: list[str],
    coa_missing: bool = False,
    archive_notes: list[str] | None = None,
    docs: list | None = None,
    channel_id: str | None = None,
) -> list:
    """Summary card posted after processing a batch of shared documents.

    Args:
        n_files:        Total files received.
        n_processed:    Docs processed without ERROR.
        workbooks:      Filenames of workbooks uploaded.
        errors:         Real failures (download / pipeline / upload) — drive the
                        warning header.
        coa_missing:    True when the client's status != "active" (no COA yet).
        archive_notes:  Background-archive hiccups. These DO NOT turn the header
                        amber — the run still succeeded for the user — and are
                        surfaced only as a muted context line.
        docs:           Optional list of processed docs (``ProcessedDoc``). When
                        provided, a per-doc detail section is rendered between the
                        header and the workbooks list. Backward-compatible default
                        is None (summary-only card).
    """
    archive_notes = archive_notes or []
    # Header line — green unless a real processing/upload error occurred.
    status_emoji = ":white_check_mark:" if not errors else ":warning:"
    header_text = (
        f"{status_emoji} *Ledgr — Batch complete*\n"
        f"Received *{n_files}* file{'s' if n_files != 1 else ''}  •  "
        f"Processed *{n_processed}* document{'s' if n_processed != 1 else ''}"
    )

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header_text},
        }
    ]

    # Per-doc detail — one card per doc, capped to keep within Slack block
    # limits. Beyond the cap, a "+N more" context line summarises the remainder.
    if docs:
        _DOC_CAP = 10
        for doc in docs[:_DOC_CAP]:
            needs_actions = (
                getattr(doc, "reconciled", True) is False
                or getattr(doc, "status", None) in ("needs_review", "failed")
            )
            doc_actions: list[str] = []
            if needs_actions:
                doc_actions = ["reextract", "edit"]
                if getattr(doc, "file_id", None):
                    doc_actions.append("view_row")
            blocks.extend(per_doc_card(doc, actions=doc_actions, channel_id=channel_id))
            # Attach feedback buttons only to clean (delivered) docs.
            # needs_review docs already have direct decision buttons — feedback is redundant.
            if not needs_actions:
                _file_id = _doc_get(doc, "file_id", "doc_key", "doc_id")
                _vendor = None
                _account_code = _doc_get(doc, "account_code")
                _tax_code = _doc_get(doc, "tax_code")
                _norm = _doc_get(doc, "normalized")
                _direction = (_doc_get(doc, "direction") or "").strip().lower()
                if _norm is not None:
                    _party = _norm.customer if _direction == "sales" else _norm.supplier
                    _vendor = getattr(_party, "name", None)
                if _vendor is None:
                    _vendor = _doc_get(doc, "counterparty")
                _doc_ref = make_feedback_doc_ref(
                    file_id=_file_id,
                    vendor=_vendor,
                    account_code=_account_code,
                    tax_code=_tax_code,
                )
                blocks.extend(feedback_buttons_block(doc_ref=_doc_ref, channel_id=channel_id))
        if len(docs) > _DOC_CAP:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"_+{len(docs) - _DOC_CAP} more document(s) — see the workbook for the full ledger._",
                        }
                    ],
                }
            )

    # Workbooks uploaded
    if workbooks:
        wb_lines = "\n".join(f"• `{wb}`" for wb in workbooks)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Workbooks uploaded:*\n{wb_lines}"},
            }
        )
    else:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_No workbooks produced._"},
            }
        )

    # COA missing note
    if coa_missing:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            ":information_source: No COA is set for this client — "
                            "account codes may be blank. Upload a COA file (.xlsx/.csv) "
                            "or run `/ledgr settings` to add one."
                        ),
                    }
                ],
            }
        )

    # Archive hiccups — muted context line only (does NOT affect the header).
    if archive_notes:
        n = len(archive_notes)
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f":file_cabinet: Your documents were processed and uploaded. "
                            f"({n} background archive step{'s' if n != 1 else ''} "
                            "didn't complete — your ledger is unaffected.)"
                        ),
                    }
                ],
            }
        )

    # Errors
    if errors:
        err_lines = "\n".join(f"• {e}" for e in errors[:10])  # cap at 10 for readability
        if len(errors) > 10:
            err_lines += f"\n_…and {len(errors) - 10} more_"
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":x: *Errors ({len(errors)}):*\n{err_lines}"},
            }
        )

    return blocks


def feedback_buttons_block(
    *,
    doc_ref: str,
    channel_id: str | None = None,
) -> list[dict]:
    """context_actions block with 👍 (learn_mapping) and 👎 (redo) feedback buttons.

    Args:
        doc_ref: ``|``-delimited string serialising (file_id, vendor, account_code,
                 tax_code). Use :func:`make_feedback_doc_ref` to build it safely.
                 Falls back to ``"-"`` when empty so Slack never rejects a blank value.
        channel_id: Used by supports_native_blocks() for per-channel probe.
    """
    safe_ref = doc_ref if doc_ref else "-"
    pos_value = f"pos|{safe_ref}"
    neg_value = f"neg|{safe_ref}"

    if supports_native_blocks(channel_id):
        return [
            {
                "type": "context_actions",
                "elements": [
                    {
                        "type": "feedback_buttons",
                        "action_id": "ledgr_doc_feedback",
                        "positive_button": {
                            "text": {"type": "plain_text", "text": "👍"},
                            "value": pos_value,
                        },
                        "negative_button": {
                            "text": {"type": "plain_text", "text": "👎"},
                            "value": neg_value,
                        },
                    }
                ],
            }
        ]

    # Fallback: two small buttons in an actions block.
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":thumbsup:", "emoji": True},
                    "action_id": "ledgr_doc_feedback_pos",
                    "value": pos_value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":thumbsdown:", "emoji": True},
                    "action_id": "ledgr_doc_feedback_neg",
                    "value": neg_value,
                },
            ],
        }
    ]


def make_feedback_doc_ref(
    *,
    file_id: str | None = None,
    vendor: str | None = None,
    account_code: str | None = None,
    tax_code: str | None = None,
) -> str:
    """Build the ``|``-delimited doc_ref string for :func:`feedback_buttons_block`.

    Each field is %-encoded to keep ``|`` safe as a delimiter.
    Falls back to ``"-"`` for any missing field so the value is never blank.
    """

    return "|".join([
        _enc(file_id),
        _enc(vendor),
        _enc(account_code),
        _enc(tax_code),
    ])


def coa_saved_blocks(n_accounts: int) -> list:
    """Confirmation card posted after a COA is successfully ingested."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":white_check_mark: Chart of accounts saved (*{n_accounts}* accounts). "
                    "This client is now active — drop documents here to get a ledger back."
                ),
            },
        }
    ]


def coa_validation_failed_blocks(errors: list[str]) -> list:
    """Error card when an uploaded COA fails validation."""
    bullets = "\n".join(f"• {e}" for e in errors)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":warning: *COA validation failed* — please fix and re-upload:\n"
                    f"{bullets}"
                ),
            },
        }
    ]


def coa_confirm_blocks(
    *,
    preview: dict,
    file_id: str,
    channel_state: str,
    filename: str = "",
) -> list:
    """Confirmation card: ask the user before persisting a COA upload.

    ``preview`` is a serialised :class:`app.coa_detect.CoaPreview` (the runner
    builds it from :func:`app.coa_detect.preview_coa`). The card shows account
    counts, a small sample, validation errors and warnings, and three buttons:

    * ``ledgr_coa_confirm`` — persist rows + activate the client.
    * ``ledgr_coa_as_document`` — fall through to the document pipeline.
    * ``ledgr_coa_cancel`` — dismiss the upload without doing anything.

    The confirm button is omitted entirely when validation has hard errors so
    the user cannot click through into a broken ingest.
    """
    n_accounts = preview.get("n_accounts", 0)
    n_income = preview.get("n_income", 0)
    n_expense = preview.get("n_expense", 0)
    sample = preview.get("sample") or []
    errors = preview.get("errors") or []
    warnings = preview.get("warnings") or []
    source = preview.get("source") or "xlsx"

    headline = (
        f":abacus: Looks like a chart of accounts — *{n_accounts}* accounts "
        f"({n_income} income · {n_expense} expense) parsed from `{source}`."
    )
    if channel_state == "active":
        headline = (
            f":abacus: Looks like a chart of accounts — *{n_accounts}* accounts. "
            "This will replace the current chart for this client."
        )

    body_sections: list = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": headline},
        }
    ]

    if filename:
        body_sections.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"File: `{filename}`"}],
            }
        )

    if sample:
        sample_lines = [
            f"• `{_enc(r.get('code') or '—')}` — {_enc(r.get('description') or '')} "
            f"({_enc(r.get('account_type') or '?')})"
            for r in sample
        ]
        body_sections.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Preview (first 5 accounts):*\n" + "\n".join(sample_lines),
                },
            }
        )

    if errors:
        bullets = "\n".join(f"• {e}" for e in errors)
        body_sections.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":warning: *Validation errors — fix the file and re-upload:*\n"
                        f"{bullets}"
                    ),
                },
            }
        )

    if warnings:
        bullets = "\n".join(f"• {w}" for w in warnings)
        body_sections.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":information_source: *Warnings:*\n{bullets}",
                },
            }
        )

    confirm_value = f"{file_id}"
    actions: list = []
    if not errors:
        if channel_state == "active":
            actions.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Replace COA"},
                    "style": "danger",
                    "action_id": "ledgr_coa_confirm",
                    "value": confirm_value,
                }
            )
        else:
            actions.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Use as COA"},
                    "style": "primary",
                    "action_id": "ledgr_coa_confirm",
                    "value": confirm_value,
                }
            )
    actions.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Process as document"},
            "action_id": "ledgr_coa_as_document",
            "value": confirm_value,
        }
    )
    actions.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Cancel"},
            "action_id": "ledgr_coa_cancel",
            "value": confirm_value,
        }
    )
    body_sections.append({"type": "actions", "elements": actions})
    return body_sections


def coa_unknown_disambiguation_blocks(*, file_id: str, filename: str = "") -> list:
    """Card for spreadsheets that don't look like a COA OR a ledger.

    Lets the user pick a path explicitly instead of guessing.
    """
    sections: list = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":grey_question: I can't tell what this spreadsheet is. "
                    "Is it a chart of accounts, or should I process it as a document?"
                ),
            },
        }
    ]
    if filename:
        sections.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"File: `{filename}`"}],
            }
        )
    value = f"{file_id}"
    sections.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Use as COA"},
                    "style": "primary",
                    "action_id": "ledgr_coa_confirm",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Process as document"},
                    "action_id": "ledgr_coa_as_document",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "action_id": "ledgr_coa_cancel",
                    "value": value,
                },
            ],
        }
    )
    return sections


def ledgr_help_blocks() -> list:
    """Usage card posted for /ledgr help or unknown subcommands."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Ledgr slash commands*\n"
                    "*/ledgr settings* — edit this client's profile\n"
                    "*/ledgr profile* — show this client's registered profile\n"
                    "*/ledgr export* — re-send the latest ledger\n"
                    "*/ledgr help* — show this message"
                ),
            },
        }
    ]


def export_unavailable_blocks() -> list:
    """Message posted when /ledgr export finds no workbooks for this channel."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "No ledger has been generated for this channel yet — "
                    "drop documents and I'll build one."
                ),
            },
        }
    ]


def job_summary_text(
    *,
    total: int,
    posted: int,
    needs_review: int = 0,
    rejected: int = 0,
    failed: int = 0,
    duplicates: int = 0,
    software: str = "",
    fy: str = "",
    kind: str = "",
) -> str:
    """One-line Job summary for a batch drop ([[Batch (Job)]] per ADR-0007).

    Posted up-front as the single top-level message for a multi-file drop; the
    per-doc status / approval cards go in-thread under it, then this summary
    is ``chat_update``-d with the final tally.

    ``kind`` ("bank" / "invoice") picks the destination noun so a bank statement
    is never mislabelled a "ledger".

    ``failed`` (transient infra errors like Gemini 503) is reported separately
    from ``rejected`` so the user can retry without believing the document was
    permanently refused.
    """
    fyl = f" FY{fy}" if fy else ""
    noun = "bank statement" if kind == "bank" else "ledger"

    parts: list[str] = []
    if posted:
        parts.append(f"{posted} posted to your{fyl} {noun}")
    if duplicates:
        parts.append(f"{duplicates} already recorded")
    if needs_review:
        parts.append(f"{needs_review} need{'s' if needs_review == 1 else ''} your review")
    if failed:
        parts.append(f"{failed} failed — retry")
    if rejected:
        parts.append(f"{rejected} rejected")

    processed = total - rejected - failed
    head = f"📥 Received {total} file{'s' if total != 1 else ''}"
    if (rejected or failed) and processed:
        head += f" · {processed} processed"
    elif not (rejected or failed):
        head = f"📥 Processed {total} document{'s' if total != 1 else ''}"

    if parts:
        return head + " — " + ", ".join(parts)
    return head + " — nothing new to add"


def job_progress_text(
    *,
    total: int,
    done: int,
    posted: int = 0,
    needs_review: int = 0,
    rejected: int = 0,
    failed: int = 0,
    duplicates: int = 0,
) -> str:
    """Live batch-drop progress line (updated after each document completes).

    ``failed`` (e.g. transient Gemini 503) is shown separately from ``rejected``
    so a retryable infrastructure failure is not labelled as a permanent reject.
    """
    if done <= 0:
        return f"📥 Received {total} document{'s' if total != 1 else ''} — starting…"
    tail: list[str] = []
    if posted:
        tail.append(f"{posted} posted")
    if duplicates:
        tail.append(f"{duplicates} already recorded")
    if needs_review:
        tail.append(f"{needs_review} need{'s' if needs_review == 1 else ''} review")
    if failed:
        tail.append(f"{failed} failed — retry")
    if rejected:
        tail.append(f"{rejected} rejected")
    detail = f" ({', '.join(tail)})" if tail else ""
    return (
        f"📥 Processing {total} document{'s' if total != 1 else ''}"
        f" — {done}/{total} done{detail}…"
    )


def delivery_card_blocks(summary: str, preview_blocks: list[dict]) -> list[dict]:
    """One delivery message: summary line + ledger preview table(s)."""
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary},
        },
    ]
    blocks.extend(preview_blocks)
    return blocks


def confident_note_block(note: str) -> dict:
    """Render a confident-path note as a Slack context block.

    Used on the no-pause delivery path when ``compose_confident_note`` produces
    a plain-language posting note (ADR-0017 Lever 1). Returns a single Slack
    ``context`` block with the note as mrkdwn so it renders beneath the main
    delivery card as a subtle annotation.
    """
    return {
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": note},
        ],
    }


def compose_batch_delivery_summary(
    *,
    groups: list[dict],
    client_name: str = "",
) -> str:
    """Aggregate delivery summary for a multi-file batch.

    Each group dict: fy, software, kind, n_rows, n_docs, client_name (optional).
    """
    if not groups:
        return "No entries were produced for this batch."

    parts: list[str] = []
    total_rows = 0
    total_docs = 0
    for g in groups:
        fy = g.get("fy", "?")
        kind = g.get("kind") or "invoice"
        sw = software_label(str(g.get("software") or ""))
        name = (g.get("client_name") or client_name or "").strip()
        doc_label = "Bank Statement" if kind == "bank" else "Ledger"
        prefix = f"{name} – " if name else ""
        dest = f"**{prefix}{doc_label} FY{fy} ({sw})**"
        n_rows = int(g.get("n_rows") or 0)
        n_docs = int(g.get("n_docs") or 0)
        total_rows += n_rows
        total_docs += n_docs
        parts.append(
            f"{n_rows} line{'s' if n_rows != 1 else ''} from "
            f"{n_docs} document{'s' if n_docs != 1 else ''} to {dest}"
        )

    if len(parts) == 1:
        return f"📒 Added {parts[0]}."
    return f"📒 Added {total_rows} lines from {total_docs} documents — " + "; ".join(parts) + "."


def approval_card_blocks(
    summary: str,
    op_id: str,
    doc_label: str | None = None,
    channel_id: str | None = None,
) -> list:
    """HITL Approve / Edit / Reject card for a document that needs human review.

    Args:
        summary: Human-readable explanation of why the document needs a decision
                 (built by the approval gate from the flagged / unreconciled lines).
        op_id:   The interrupt id correlating this card with the paused workflow;
                 carried as each button's ``value`` so the action handler can
                 resume the right session.
        doc_label: Optional human label tying this card to the uploaded document
                 (e.g. ``"📄 Receipt-Hotel.pdf · Hotel Booking · $51.49"``). When
                 supplied, it is rendered as the leading line of the header so a
                 user dropping many documents can tell the cards apart. None (or
                 absent) preserves the original card layout for backward compat.
        channel_id: Used by supports_native_blocks() for per-channel probe.
    """
    approve_btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": "Approve", "emoji": True},
        "action_id": "approve",
        "style": "primary",
        "value": op_id,
    }
    edit_btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": "Edit", "emoji": True},
        "action_id": "edit",
        "value": op_id,
    }
    reject_btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": "Reject", "emoji": True},
        "action_id": "reject",
        "style": "danger",
        "value": op_id,
    }

    if supports_native_blocks(channel_id):
        title_text = "📒 Review needed before adding to the ledger"
        subtitle_text = doc_label or ""
        body_text = _truncate(summary, _MAX_NATIVE_CARD_BODY)
        card: dict = {
            "type": "card",
            "title": {"type": "mrkdwn", "text": title_text},
            "actions": [approve_btn, edit_btn, reject_btn],
        }
        if subtitle_text:
            card["subtitle"] = {"type": "mrkdwn", "text": subtitle_text}
        if body_text:
            card["body"] = {"type": "mrkdwn", "text": body_text}
        blocks: list[dict] = [card]
        # If summary was truncated, emit the full text in a context block.
        if len(summary) > _MAX_NATIVE_CARD_BODY:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": summary}],
                }
            )
        return blocks

    # Fallback: existing section + actions shape (unchanged).
    header = ":mag: *Review needed before adding to the ledger*"
    if doc_label:
        header = f"{doc_label}\n{header}"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{header}\n{summary}",
            },
        },
        {
            "type": "actions",
            "block_id": "ledgr_approval",
            "elements": [approve_btn, edit_btn, reject_btn],
        },
    ]


def approval_outcome_blocks(summary: str, decision: str) -> list:
    """Replacement card (via ``chat_update``) showing the resolved HITL outcome."""
    icon = {"approve": ":white_check_mark:", "edit": ":pencil2:", "reject": ":x:"}.get(
        decision, ":information_source:"
    )
    verb = {"approve": "Approved", "edit": "Approved with edits", "reject": "Rejected"}.get(
        decision, decision.title()
    )
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{icon} *{verb}.* {summary}"},
        }
    ]


_REVIEW_CARD_DEFAULT_QUESTION = (
    "This document needs your review before it's added to the ledger."
)


def review_card_blocks(
    question: str,
    op_id: str,
    reasons: list[str] | None = None,
    channel_id: str | None = None,
) -> list:
    """Mid-flow HITL card for a :review interrupt from ``review_extraction_node``.

    Shows the reviewer's precise question + a short bullet summary of the
    struggle signals that triggered the escalation.  Three buttons let the human
    steer the re-extraction without ever touching the ledger:

    * ``review_reextract`` — opens a hint-input modal so the human can describe
      what the extractor missed (e.g. "this is a tax invoice, not a receipt").
    * ``review_confirm`` — waves the current extraction through unchanged.
    * ``review_reject`` — drops the document entirely (mirrors the approval Reject).

    Each button carries ``op_id`` (the ``:review`` interrupt id) as its ``value``
    so the action handler can resume the correct paused session.

    Args:
        question: Human-facing question produced by ``review_extraction_node``
                  (stored in ``state["review_question"]``).
        op_id:    The ``:review`` interrupt id.
        reasons:  List of struggle signals from ``state[REVIEW_REASON_KEY]``.
                  Rendered as bullets under the question; ``None`` or empty hides
                  the bullets section.
        channel_id: Used by supports_native_blocks() for per-channel probe.
    """
    question = question.strip() if question else ""
    if not question:
        question = _REVIEW_CARD_DEFAULT_QUESTION

    reextract_btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": "Re-extract with a hint", "emoji": True},
        "action_id": "review_reextract",
        "style": "primary",
        "value": op_id,
    }
    confirm_btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": "Looks right, keep it", "emoji": True},
        "action_id": "review_confirm",
        "value": op_id,
    }
    reject_btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": "Reject this doc", "emoji": True},
        "action_id": "review_reject",
        "style": "danger",
        "value": op_id,
    }

    if supports_native_blocks(channel_id):
        title_text = "🔍 Extraction needs your input"
        body_text = _truncate(question, _MAX_NATIVE_CARD_BODY)
        card: dict = {
            "type": "card",
            "title": {"type": "mrkdwn", "text": title_text},
            "body": {"type": "mrkdwn", "text": body_text},
            "actions": [reextract_btn, confirm_btn, reject_btn],
        }
        blocks: list[dict] = [card]
        # Full question overflow goes into context block.
        if len(question) > _MAX_NATIVE_CARD_BODY:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": question}],
                }
            )
        # Struggle reasons always go into a separate context block (untruncated).
        if reasons:
            bullets = "\n".join(f"• {r}" for r in reasons)
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f"*Signals detected:*\n{bullets}"}
                    ],
                }
            )
        return blocks

    # Fallback: existing section + actions shape (unchanged).
    header = ":mag: *Extraction needs your input*"
    body = question
    if reasons:
        bullets = "\n".join(f"  • {r}" for r in reasons)
        body = f"{question}\n\n*Signals detected:*\n{bullets}"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{header}\n{body}",
            },
        },
        {
            "type": "actions",
            "block_id": "ledgr_review",
            "elements": [reextract_btn, confirm_btn, reject_btn],
        },
    ]
    return blocks


def review_outcome_blocks(question: str, action: str) -> list:
    """Replacement card (via ``chat_update``) showing the resolved review outcome.

    Args:
        question: The original reviewer question (carried from the interrupt doc).
        action:   One of ``"reextract_as"``, ``"confirm_as_is"``, or ``"reject"``.
    """
    icon = {
        "reextract_as": ":arrows_counterclockwise:",
        "confirm_as_is": ":white_check_mark:",
        "reject": ":x:",
    }.get(action, ":information_source:")
    verb = {
        "reextract_as": "Re-extracting with your hint",
        "confirm_as_is": "Extraction accepted — continuing",
        "reject": "Rejected",
    }.get(action, action.replace("_", " ").title())
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{icon} *{verb}.* {question}"},
        }
    ]


def review_hint_modal(op_id: str) -> dict:
    """Modal for the 'Re-extract with a hint' button.

    A single plain-text input collects the human's free-text hint.  The modal's
    ``private_metadata`` carries ``op_id`` so the view-submission handler can
    resume the correct session.  ``callback_id`` = ``"ledgr_review_hint"`` so
    the Bolt ``@app.view`` decorator can route it.
    """
    return {
        "type": "modal",
        "callback_id": "ledgr_review_hint",
        "private_metadata": op_id,
        "title": {"type": "plain_text", "text": "Hint for re-extraction", "emoji": True},
        "submit": {"type": "plain_text", "text": "Re-extract", "emoji": True},
        "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
        "blocks": [
            {
                "type": "input",
                "block_id": "hint_block",
                "label": {
                    "type": "plain_text",
                    "text": "What should the extractor know?",
                    "emoji": True,
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "hint_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. 'This is a tax invoice. The supplier is GST-registered.'",
                    },
                },
            }
        ],
    }


#: Maps the STABLE machine reason prefixes produced by ``detect_struggle``
#: (nodes.py) to a friendly human phrase. The reviewer reasons can carry a
#: ``"<prefix>: <label> (<note>)"`` suffix, so we match on the leading prefix and
#: drop the audit detail for the proactive offer card.
_PROACTIVE_REASON_PHRASES = {
    "unreconciled": "the totals didn't reconcile",
    "doc_type_other": "the document type was unclear",
    "bundle_empty": "I couldn't find any line items",
    "lines_empty": "a document had no line items",
    "low_classify_confidence": "I wasn't confident how to categorise it",
    "missing_required": "some required fields were missing",
}


def _humanize_review_reason(reason: str) -> str:
    """Turn one STABLE machine reason string into a friendly phrase.

    ``detect_struggle`` emits reasons like ``"unreconciled: Invoice (FX off by
    0.02)"`` or ``"low_classify_confidence"``.  We key off the prefix before the
    first ``":"`` and fall back to a de-underscored form for any future signal we
    don't have an explicit phrase for.
    """
    prefix = (reason or "").split(":", 1)[0].strip()
    phrase = _PROACTIVE_REASON_PHRASES.get(prefix)
    if phrase:
        return phrase
    return prefix.replace("_", " ") or "something looked off"


def proactive_redo_blocks(
    file_id: str,
    reasons: list[str] | None = None,
    channel_id: str | None = None,
) -> list:
    """Post-delivery offer card: this doc looked off — want me to re-read it?

    Surfaced AFTER a flagged document has already been filed (the extract
    reviewer fired but the doc was delivered without pausing the user).  Names
    what looked off in friendly language and offers a single button that opens a
    hint-input modal to re-extract.  The button ``value`` carries ``file_id`` so
    the action handler can open the modal for the right document.

    Args:
        file_id: Slack file id of the delivered document (button value).
        reasons: STABLE machine reason strings from ``state[REVIEW_REASON_KEY]``.
                 Humanized + de-duplicated for the friendly line.
        channel_id: Used by supports_native_blocks() for per-channel probe.
    """
    phrases: list[str] = []
    for r in reasons or []:
        phrase = _humanize_review_reason(r)
        if phrase not in phrases:
            phrases.append(phrase)
    if phrases:
        detail = "; ".join(phrases)
        body = (
            f":thinking_face: This one looked a little off — {detail}. "
            "I filed it anyway, but want me to re-read it with a hint?"
        )
    else:
        body = (
            ":thinking_face: This one looked a little off. I filed it anyway, "
            "but want me to re-read it with a hint?"
        )

    reextract_btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": "Re-extract with a hint", "emoji": True},
        "action_id": "proactive_redo",
        "value": file_id,
    }

    if supports_native_blocks(channel_id):
        title_text = "🔄 Want to re-extract this with a hint?"
        body_text = _truncate(body, _MAX_NATIVE_CARD_BODY)
        card: dict = {
            "type": "card",
            "title": {"type": "mrkdwn", "text": title_text},
            "body": {"type": "mrkdwn", "text": body_text},
            "actions": [reextract_btn],
        }
        blocks: list[dict] = [card]
        # If body was truncated, emit full text in context block.
        if len(body) > _MAX_NATIVE_CARD_BODY:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": body}],
                }
            )
        return blocks

    # Fallback: existing section + actions shape (unchanged).
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
        },
        {
            "type": "actions",
            "block_id": "ledgr_proactive_redo",
            "elements": [reextract_btn],
        },
    ]


def proactive_redo_modal(file_id: str) -> dict:
    """Hint-input modal for the proactive 'Re-extract with a hint' button.

    Mirrors :func:`review_hint_modal` but for the POST-delivery proactive path:
    there is no paused interrupt to resume, so ``private_metadata`` carries the
    ``file_id`` (not an ``op_id``) and the ``callback_id`` is
    ``"ledgr_proactive_redo"`` so the Bolt ``@app.view`` decorator routes it to
    the proactive re-extract handler.
    """
    return {
        "type": "modal",
        "callback_id": "ledgr_proactive_redo",
        "private_metadata": file_id,
        "title": {"type": "plain_text", "text": "Re-read this document", "emoji": True},
        "submit": {"type": "plain_text", "text": "Re-extract", "emoji": True},
        "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
        "blocks": [
            {
                "type": "input",
                "block_id": "hint_block",
                "label": {
                    "type": "plain_text",
                    "text": "What should the extractor know?",
                    "emoji": True,
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "hint_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. 'This is a tax invoice. The supplier is GST-registered.'",
                    },
                },
            }
        ],
    }


def coa_prompt_blocks() -> list:
    """Blocks posted in-channel after a profile is saved, asking for COA."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "✅ Profile saved. Drop your COA file (.xlsx/.csv) here to "
                    "activate this client. You can still drop documents anytime — "
                    "categorisation starts once a valid COA is uploaded."
                ),
            },
        },
    ]


_UNMAPPED_ACCT_OPTION = {
    "text": {"type": "plain_text", "text": "UNMAPPED — assign later"},
    "value": "",
}


def _line_account_select_options(
    ln: dict,
    coa_options: list[tuple[str, str]],
) -> list[dict]:
    """Build static_select options for one invoice line (WS-3.5).

    Flagged lines show LLM ``account_alternative_codes`` plus an UNMAPPED
    abstention option. Other lines use the full client COA list.
    """
    by_code = {code: lbl for code, lbl in coa_options}
    if ln.get("account_flagged"):
        seen: set[str] = set()
        options: list[dict] = []
        current = (ln.get("account_code") or "").strip()
        if current and current not in seen:
            lbl = by_code.get(current, current)
            options.append({"text": {"type": "plain_text", "text": lbl[:75]}, "value": current})
            seen.add(current)
        for code in ln.get("account_alternative_codes") or []:
            if not code or code in seen:
                continue
            seen.add(code)
            lbl = by_code.get(code, code)
            options.append({"text": {"type": "plain_text", "text": lbl[:75]}, "value": code})
        options.append(dict(_UNMAPPED_ACCT_OPTION))
        return options
    if not coa_options:
        return []
    return [
        {"text": {"type": "plain_text", "text": lbl[:75]}, "value": code}
        for code, lbl in coa_options
    ]


def invoice_edit_modal(op_id: str, lines: list[dict], coa_options: list[tuple[str, str]]) -> dict:
    """Modal to correct each flagged line's account code / tax treatment / net amount.

    ``coa_options`` is a list of (code, label) for the static_select; ``lines`` is
    the proposed extraction. ``block_id`` encodes the line index: ``acct_<i>`` etc.

    The modal's initial values are pulled from the canonical ``InvoiceLine`` keys
    (``tax_treatment`` and ``net_amount``) so the values pre-populate from the
    extractor's actual output and a round-tripped edit lands on the canonical
    keys the exporter writes from.
    """
    tax_opts = [{"text": {"type": "plain_text", "text": t}, "value": t}
                for t in ("SR", "ZR", "ES", "TX", "OS")]
    blocks: list = []
    # Modal exposes the subset of nodes.EDITABLE_LINE_FIELDS that users can
    # actually correct in-place (account_code/tax_treatment/net_amount). The
    # line description is shown read-only as the section header above each
    # group.
    for i, ln in enumerate(lines):
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"*Line {i + 1}: {ln.get('description', '')}*"}})
        # Slack rejects a static_select with empty options, which would make the
        # whole modal fail to open. When the client has no COA, omit the
        # account-code block entirely — tax and amount stay editable.
        line_coa = _line_account_select_options(ln, coa_options)
        if line_coa:
            acct_initial = next((o for o in line_coa if o["value"] == ln.get("account_code")), None)
            blocks.append({
                "type": "input", "block_id": f"acct_{i}", "optional": True,
                "label": {"type": "plain_text", "text": "Account code"},
                "element": {"type": "static_select", "action_id": "v", "options": line_coa,
                            **({"initial_option": acct_initial} if acct_initial else {})},
            })
        tax_initial = next((o for o in tax_opts if o["value"] == ln.get("tax_treatment")), None)
        blocks.append({
            "type": "input", "block_id": f"tax_{i}", "optional": True,
            "label": {"type": "plain_text", "text": "Tax treatment"},
            "element": {"type": "static_select", "action_id": "v", "options": tax_opts,
                        **({"initial_option": tax_initial} if tax_initial else {})},
        })
        blocks.append({
            "type": "input", "block_id": f"amt_{i}", "optional": True,
            "label": {"type": "plain_text", "text": "Net amount"},
            "element": {"type": "number_input", "action_id": "v", "is_decimal_allowed": True,
                        **({"initial_value": str(ln["net_amount"])} if ln.get("net_amount") is not None else {})},
        })
    return {
        "type": "modal", "callback_id": "ledgr_invoice_edit", "private_metadata": op_id,
        "title": {"type": "plain_text", "text": "Review invoice"},
        "submit": {"type": "plain_text", "text": "Post to ledger"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def _dedup_value(vendor: str, fy: int, month: str, op_id: str | None) -> str:
    # Format: vendor|fy|month|op_id  — month/vendor are %-encoded so "|" is safe as delimiter.
    # op_id falls back to "-" so button value is never empty (Slack rejects "value": "").
    return "|".join([
        urllib.parse.quote(vendor, safe="") or "-",
        str(fy) or "0",
        urllib.parse.quote(month, safe="") or "-",
        urllib.parse.quote(op_id or "-", safe=""),
    ])


def dedup_callout_card(
    *,
    vendor: str,
    fy: int,
    month: str,
    existing: dict,
    incoming: dict,
    op_id: str | None = None,
    channel_id: str | None = None,
) -> list[dict]:
    """Yellow-warning card posted when the dedup guard finds a duplicate month/vendor.

    Args:
        vendor:   Vendor or counterparty name.
        fy:       Financial year integer (e.g. 2025).
        month:    Human month label (e.g. "September 2025").
        existing: ``{"rows": int, "date_range": str, "workbook": str}`` for what's recorded.
        incoming: ``{"rows": int, "date_range": str, "file_label": str}`` for the new file.
        op_id:    Optional run/interrupt id threaded into button values.
        channel_id: Used by supports_native_blocks() for per-channel probe.
    """
    title_text = f"⚠️ I already have *{month}* invoices for {vendor}"
    subtitle_text = f"FY{fy} · {existing.get('workbook', '')}"
    body_raw = (
        f"*Existing:* {existing.get('rows', 0)} rows · {existing.get('date_range', '—')}\n"
        f"*Incoming:* {incoming.get('rows', 0)} rows · {incoming.get('date_range', '—')}"
    )
    body_text = body_raw if len(body_raw) <= _MAX_NATIVE_CARD_BODY else body_raw[: _MAX_NATIVE_CARD_BODY - 1] + "…"

    btn_value = _dedup_value(vendor, fy, month, op_id)
    replace_btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": "Replace recorded month"},
        "style": "danger",
        "action_id": "ledgr_dedup_replace",
        "value": btn_value,
    }
    keep_btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": "Keep existing"},
        "action_id": "ledgr_dedup_keep",
        "value": btn_value,
    }

    if supports_native_blocks(channel_id):
        return [
            {
                "type": "card",
                "title": {"type": "mrkdwn", "text": title_text},
                "subtitle": {"type": "mrkdwn", "text": subtitle_text},
                "body": {"type": "mrkdwn", "text": body_text},
                "actions": [replace_btn, keep_btn],
            }
        ]

    # Fallback: section with concatenated text + actions block.
    fallback_text = f"{title_text}\n{subtitle_text}\n{body_text}"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": fallback_text},
        },
        {
            "type": "actions",
            "elements": [replace_btn, keep_btn],
        },
    ]


_DATA_TABLE_MAX_ROWS = 100  # Slack hard cap for data_table rows (excl. header)


def summary_table_blocks(
    summary_table: list[dict],
    *,
    channel_id: str | None = None,
    title: str = "Document summary",
) -> list[dict]:
    """Drive-style Category / Details table for human review before approval."""
    if not summary_table:
        return []

    if supports_native_blocks(channel_id):
        header_row = [
            {"type": "raw_text", "text": "Category"},
            {"type": "raw_text", "text": "Details"},
        ]
        data_rows: list[list[dict]] = []
        for row in summary_table[:_DATA_TABLE_MAX_ROWS]:
            cat = str(row.get("category") or "—").strip() or "—"
            det = str(row.get("details") or "—").strip() or "—"
            data_rows.append([
                {"type": "raw_text", "text": cat},
                {"type": "raw_text", "text": det},
            ])
        blocks: list[dict] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{title}*"},
            },
            {
                "type": "data_table",
                "caption": title,
                "rows": [header_row, *data_rows],
            },
        ]
        return blocks

    lines = [f"*{title}*"]
    for row in summary_table[:20]:
        cat = str(row.get("category") or "—").strip() or "—"
        det = str(row.get("details") or "—").strip() or "—"
        lines.append(f"• *{cat}*: {det}")
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        }
    ]


def ledger_preview_data_table(
    *,
    rows: list[dict],
    workbook_name: str,
    fy: int,
    sheet: str = "Purchase",
    software: str = "qbs_ledger",
    channel_id: str | None = None,
    max_rows: int = 10,
) -> list[dict]:
    """Build a preview of the last rows appended to the FY ledger.

    The column shape mirrors the .xlsx the user will download — Xero columns
    for Xero workbooks, QBS Ledger columns for QBS, and a 6-col bank shape
    for bank sheets (anything whose ``sheet`` is not ``"Purchase"`` or
    ``"Sales"``).

    Native path: a ``data_table`` block with per-software/per-sheet columns,
    optionally preceded by a labelling section and followed by a context block
    when ``len(rows) > max_rows``.

    Fallback path: a single ``section`` block with a fixed-width mrkdwn
    pre-block showing the first text column, a description column, and the
    last numeric column (total/balance), capped at 3000 chars.

    Args:
        rows:          List of exporter row dicts (same dicts SlackLedgerStore
                       receives — not a parallel/flattened shape).
        workbook_name: Display name of the workbook (e.g. ``"Ledger_FY2025.xlsx"``).
        fy:            Financial year integer (e.g. 2025).
        sheet:         Sheet name: ``"Purchase"``, ``"Sales"``, or a bank
                       account name.  Drives column spec selection.
        software:      Accounting software key (``"xero"``, ``"qbs_ledger"``,
                       ``"qbs"``, ``"QBS Ledger"``, ``"Xero"``, etc.).
        channel_id:    Used by supports_native_blocks() for per-channel probe.
        max_rows:      Maximum data rows to show in the preview (default 10).
                       Capped internally at 100 (Slack's data_table row limit).
    """
    if not rows:
        return []

    # Belt-and-suspenders: never emit a data_table wider than Slack's 20-column
    # limit, no matter what the spec source produced. When capping, keep the
    # first 19 columns plus the LAST (typically the amount/total) rather than
    # dropping the tail.
    col_spec = preview_column_spec(software=software, sheet=sheet)
    if len(col_spec) > SLACK_DATA_TABLE_MAX_COLS:
        col_spec = [*col_spec[: SLACK_DATA_TABLE_MAX_COLS - 1], col_spec[-1]]
    sw_label = software_label(software)
    effective_max = min(max_rows, _DATA_TABLE_MAX_ROWS)
    preview = rows[:effective_max]
    overflow = len(rows) - effective_max

    if supports_native_blocks(channel_id):
        header_row = [
            {"type": "raw_text", "text": col.header} for col in col_spec
        ]

        data_rows: list[list[dict]] = []
        for row in preview:
            cells: list[dict] = []
            for col in col_spec:
                val = row.get(col.row_key)
                if col.cell_type == "raw_number":
                    try:
                        f = float(val)  # type: ignore[arg-type]
                        cells.append({"type": "raw_number", "text": f"{f:.2f}", "value": f})
                    except (TypeError, ValueError):
                        # Missing/non-numeric: Slack rejects empty raw_text cells.
                        cells.append({"type": "raw_text", "text": "—"})
                else:
                    text = str(val).strip() if val is not None else ""
                    cells.append({"type": "raw_text", "text": text or "—"})
            data_rows.append(cells)

        caption = (
            f"{sheet} — {len(preview)} row{'s' if len(preview) != 1 else ''} added"
            f" · FY{fy} · {sw_label}"
        )
        blocks: list[dict] = [
            {
                "type": "data_table",
                "caption": caption,
                "page_size": 10,
                "row_header_column_index": 0,
                "rows": [header_row] + data_rows,
            },
        ]

        if overflow > 0:
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"… and {overflow} more rows in the workbook above.",
                    }
                ],
            })

        return blocks

    # Fallback: fixed-width mrkdwn pre-block.
    # Use first text col, second text col (description-ish), last numeric col.
    text_cols = [c for c in col_spec if c.cell_type == "raw_text"]
    num_cols = [c for c in col_spec if c.cell_type == "raw_number"]
    key_date = text_cols[0].row_key if text_cols else "Date"
    key_desc = text_cols[1].row_key if len(text_cols) > 1 else "Description"
    key_total = num_cols[-1].row_key if num_cols else "Total"
    _COL_WIDTHS = (12, 36, 12)
    header_line = (
        f"{'Date':<{_COL_WIDTHS[0]}}  "
        f"{'Description':<{_COL_WIDTHS[1]}}  "
        f"{'Amount':>{_COL_WIDTHS[2]}}"
    )
    separator = "-" * (sum(_COL_WIDTHS) + 4)
    lines = [header_line, separator]
    for row in preview:
        date_s = str(row.get(key_date) or "")[:_COL_WIDTHS[0]]
        desc_s = str(row.get(key_desc) or "")[:_COL_WIDTHS[1]]
        try:
            total_f = float(row.get(key_total) or 0)  # type: ignore[arg-type]
            amt_s = f"{total_f:>{_COL_WIDTHS[2]}.2f}"
        except (TypeError, ValueError):
            amt_s = " " * _COL_WIDTHS[2]
        lines.append(
            f"{date_s:<{_COL_WIDTHS[0]}}  {desc_s:<{_COL_WIDTHS[1]}}  {amt_s}"
        )
    if overflow > 0:
        lines.append(f"… and {overflow} more rows in the workbook above.")
    table_text = "```\n" + "\n".join(lines) + "\n```"
    # Slack section text ceiling is 3000 chars; truncate if needed.
    if len(table_text) > _MAX_SECTION:
        table_text = table_text[: _MAX_SECTION - 4] + "\n```"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Recent {sheet} rows in *{workbook_name}* (FY{fy}):\n{table_text}",
            },
        }
    ]


def profile_summary_blocks(profile: dict) -> list:
    """Confirmation card summarising the client profile that was just registered."""
    name = profile.get("client_name") or "(unnamed client)"
    software = profile.get("accounting_software") or "—"
    raw = profile.get("fye_month")
    try:
        fye_num = int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        fye_num = None
    fye = next((n for num, n in _MONTHS if num == fye_num), "—")
    gst = "GST-registered" if profile.get("gst_registered") else "Not GST-registered"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"✅ *Client registered: {name}*\n"
                    f"• Accounting software: *{software}*\n"
                    f"• Financial year-end: *{fye}*\n"
                    f"• GST status: *{gst}*"
                ),
            },
        }
    ]
