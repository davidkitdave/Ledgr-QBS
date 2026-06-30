"""Pure Block Kit builders — return dicts/lists, no Slack API calls."""

from __future__ import annotations

import urllib.parse

from app.native_blocks_compat import supports_native_blocks
from ledgr_slack.jurisdiction import supported_regions
from ledgr_slack.export.exporters import (
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


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


_MAX_CARD_BODY = 500
# Slack native ``card`` block body/subtitle hard limit (API rejects at 201).
_MAX_NATIVE_CARD_BODY = 200


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


_UNMAPPED_ACCT_OPTION = {
    "text": {"type": "plain_text", "text": "UNMAPPED — assign later"},
    "value": "",
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
