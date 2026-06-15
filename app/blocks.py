"""Pure Block Kit builders — return dicts/lists, no Slack API calls."""

from __future__ import annotations

import urllib.parse

from app.native_blocks_compat import supports_native_blocks

# Ordered pipeline stage keys and their display titles.
PIPELINE_STAGES: tuple[str, ...] = (
    "classify",
    "extract",
    "categorize",
    "tax",
    "approve",
)

_STAGE_TITLES: dict[str, str] = {
    "classify": "Classifying",
    "extract": "Extracting",
    "categorize": "Categorizing",
    "tax": "Applying tax",
    "approve": "Awaiting approval",
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


_MONTHS = [
    (1, "January"), (2, "February"), (3, "March"), (4, "April"),
    (5, "May"), (6, "June"), (7, "July"), (8, "August"),
    (9, "September"), (10, "October"), (11, "November"), (12, "December"),
]

_SOFTWARE_OPTIONS = ["QBS Ledger", "Xero"]


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
    """Build the 4-field onboarding modal view dict.

    Args:
        prefill: optional dict with keys client_name, fye_month, accounting_software,
                 gst_registered (bool) to pre-populate the modal for /ledgr settings.
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

    # --- block 2: fye_month ---
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

    # --- block 3: accounting_software ---
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

    # --- block 4: gst_registered ---
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
        "blocks": [client_name_block, fye_block, software_block, gst_block],
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


def _fmt_money(amount: float | None, currency: str = "SGD") -> str:
    """Format a document total with its currency, e.g. '$1,234.50' / 'SGD 1,234.50'."""
    if amount is None:
        return "—"
    cur = (currency or "SGD").strip().upper()
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
    currency = getattr(norm, "currency", None) or "SGD"

    parts = [f"*{counterparty}*"]
    if inv_no:
        parts.append(f"#{inv_no}")
    if inv_date is not None:
        parts.append(inv_date.isoformat() if hasattr(inv_date, "isoformat") else str(inv_date))
    parts.append(_fmt_money(total, currency))
    return _clamp_section(f"{marker}:page_facing_up: " + "  •  ".join(parts) + dest)


_MAX_CARD_TITLE = 150
_MAX_CARD_BODY = 200


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
        if counterparty is not None:
            title_raw = counterparty.strip() or "Unknown"
            inv_no = _doc_get(doc, "invoice_number")
            inv_date = _doc_get(doc, "invoice_date")
            date_str = str(inv_date) if inv_date else ""
            currency = _doc_get(doc, "currency") or "SGD"
            total = _doc_get(doc, "total")
            tax_code = _doc_get(doc, "tax_code")
            account_code = _doc_get(doc, "account_code")
        else:
            # Object-style ProcessedDoc
            norm = _doc_get(doc, "normalized")
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
            currency = getattr(norm, "currency", None) or "SGD"
            total = getattr(norm, "doc_total", None)
            tax_code = None
            account_code = None

        subtitle_parts = []
        if inv_no:
            subtitle_parts.append(f"Invoice #{inv_no}")
        if date_str:
            subtitle_parts.append(date_str)
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
        card["body"] = {"type": "mrkdwn", "text": _truncate(full_body, _MAX_CARD_BODY)}

    # reconciled check: plain dicts use key "reconciled", objects use attribute.
    reconciled = _doc_get(doc, "reconciled")
    needs_review = reconciled is False
    if needs_review:
        note = (_doc_get(doc, "note") or "").strip()
        if note.upper().startswith("ERROR"):
            label = "failed to process"
        else:
            label = "needs review"
        reason = note if len(note) <= _MAX_CARD_BODY else note[: _MAX_CARD_BODY - 1] + "…"
        subtext = f"{label} — {reason}" if reason else label
        subtext_val = subtext[: _MAX_CARD_BODY] if len(subtext) > _MAX_CARD_BODY else subtext
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
                            "account codes may be blank. Upload a COA file or tap "
                            "*Use standard SG SME COA* to activate full categorisation."
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
    if rejected:
        parts.append(f"{rejected} rejected")

    processed = total - rejected
    head = f"📥 Received {total} file{'s' if total != 1 else ''}"
    if rejected and processed:
        head += f" · {processed} processed"
    elif not rejected:
        head = f"📥 Processed {total} document{'s' if total != 1 else ''}"

    if parts:
        return head + " — " + ", ".join(parts)
    return head + " — nothing new to add"


def approval_card_blocks(summary: str, op_id: str, doc_label: str | None = None) -> list:
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
    """
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
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                    "action_id": "approve",
                    "style": "primary",
                    "value": op_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit", "emoji": True},
                    "action_id": "edit",
                    "value": op_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject", "emoji": True},
                    "action_id": "reject",
                    "style": "danger",
                    "value": op_id,
                },
            ],
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


def review_card_blocks(question: str, op_id: str, reasons: list[str] | None = None) -> list:
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
    """
    header = ":mag: *Extraction needs your input*"
    body = question
    if reasons:
        bullets = "\n".join(f"  • {r}" for r in reasons)
        body = f"{question}\n\n*Signals detected:*\n{bullets}"

    blocks: list[dict] = [
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
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Re-extract with a hint", "emoji": True},
                    "action_id": "review_reextract",
                    "style": "primary",
                    "value": op_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Looks right, keep it", "emoji": True},
                    "action_id": "review_confirm",
                    "value": op_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject this doc", "emoji": True},
                    "action_id": "review_reject",
                    "style": "danger",
                    "value": op_id,
                },
            ],
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


def proactive_redo_blocks(file_id: str, reasons: list[str] | None = None) -> list:
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
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
        },
        {
            "type": "actions",
            "block_id": "ledgr_proactive_redo",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Re-extract with a hint", "emoji": True},
                    "action_id": "proactive_redo",
                    "value": file_id,
                }
            ],
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
                    "✅ Profile saved. Drop your COA file (.xlsx/.csv) here, "
                    "or tap *Use standard SG SME COA*"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Use standard SG SME COA", "emoji": True},
                    "action_id": "ledgr_use_standard_coa",
                }
            ],
        },
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
    coa = [{"text": {"type": "plain_text", "text": lbl[:75]}, "value": code}
           for code, lbl in coa_options]
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
        if coa:
            acct_initial = next((o for o in coa if o["value"] == ln.get("account_code")), None)
            blocks.append({
                "type": "input", "block_id": f"acct_{i}", "optional": True,
                "label": {"type": "plain_text", "text": "Account code"},
                "element": {"type": "static_select", "action_id": "v", "options": coa,
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
        op_id or "-",
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
    body_text = body_raw if len(body_raw) <= _MAX_CARD_BODY else body_raw[: _MAX_CARD_BODY - 1] + "…"

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
