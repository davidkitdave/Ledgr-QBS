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


def result_card(
    *,
    n_files: int,
    n_processed: int,
    workbooks: list[str],
    errors: list[str],
    coa_missing: bool = False,
    archive_notes: list[str] | None = None,
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


def approval_card_blocks(summary: str, op_id: str) -> list:
    """HITL Approve / Edit / Reject card for a document that needs human review.

    Args:
        summary: Human-readable explanation of why the document needs a decision
                 (built by the approval gate from the flagged / unreconciled lines).
        op_id:   The interrupt id correlating this card with the paused workflow;
                 carried as each button's ``value`` so the action handler can
                 resume the right session.
    """
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":mag: *Review needed before adding to the ledger*\n{summary}",
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
