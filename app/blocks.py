"""Pure Block Kit builders — return dicts/lists, no Slack API calls."""

from __future__ import annotations

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


def result_card(
    *,
    n_files: int,
    n_processed: int,
    workbooks: list[str],
    errors: list[str],
    coa_missing: bool = False,
    archive_notes: list[str] | None = None,
    docs: list | None = None,
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

    # Per-doc detail — one section per doc, capped to keep within Slack block
    # limits. Beyond the cap, a "+N more" context line summarises the remainder.
    if docs:
        _DOC_CAP = 10
        for doc in docs[:_DOC_CAP]:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": _per_doc_line(doc)},
                }
            )
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
) -> str:
    """One-line Job summary for a batch drop ([[Batch (Job)]] per ADR-0007).

    Posted up-front as the single top-level message for a multi-file drop; the
    per-doc status / approval cards go in-thread under it, then this summary
    is ``chat_update``-d with the final tally.
    """
    tgt = f" {software}" if software else ""
    fyl = f" FY{fy}" if fy else ""

    parts: list[str] = []
    if posted:
        parts.append(f"{posted} posted to your{tgt}{fyl} ledger")
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
    """Modal to correct each flagged line's account code / tax code / amount.

    ``coa_options`` is a list of (code, label) for the static_select; ``lines`` is
    the proposed extraction. ``block_id`` encodes the line index: ``acct_<i>`` etc.
    """
    coa = [{"text": {"type": "plain_text", "text": lbl[:75]}, "value": code}
           for code, lbl in coa_options]
    tax_opts = [{"text": {"type": "plain_text", "text": t}, "value": t}
                for t in ("SR", "ZR", "ES", "TX", "OS")]
    blocks: list = []
    # Modal exposes the subset of nodes.EDITABLE_LINE_FIELDS that users can
    # actually correct in-place (account_code/tax_code/amount). The line
    # description is shown read-only as the section header above each group.
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
        tax_initial = next((o for o in tax_opts if o["value"] == ln.get("tax_code")), None)
        blocks.append({
            "type": "input", "block_id": f"tax_{i}", "optional": True,
            "label": {"type": "plain_text", "text": "Tax code"},
            "element": {"type": "static_select", "action_id": "v", "options": tax_opts,
                        **({"initial_option": tax_initial} if tax_initial else {})},
        })
        blocks.append({
            "type": "input", "block_id": f"amt_{i}", "optional": True,
            "label": {"type": "plain_text", "text": "Amount"},
            "element": {"type": "number_input", "action_id": "v", "is_decimal_allowed": True,
                        **({"initial_value": str(ln["amount"])} if ln.get("amount") is not None else {})},
        })
    return {
        "type": "modal", "callback_id": "ledgr_invoice_edit", "private_metadata": op_id,
        "title": {"type": "plain_text", "text": "Review invoice"},
        "submit": {"type": "plain_text", "text": "Post to ledger"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


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
