"""Block Kit builders for /ledgr credits and App Home credit surfaces."""

from __future__ import annotations

from typing import Any

from ledgr_slack.credits_report import channel_usage, read_firm_balance, usage_by_channel

_COIN = "🪙"


def format_coin_balance(balance: int) -> str:
    unit = "credit" if balance == 1 else "credits"
    return f"{_COIN} {balance} {unit}"


def format_coin_footer(*, credits_used: int, credits_remaining: int) -> str:
    used_unit = "credit" if credits_used == 1 else "credits"
    return f"{_COIN} Used {credits_used} {used_unit} · {credits_remaining} remaining"


def credit_footer_block(*, credits_used: int, credits_remaining: int) -> dict:
    return {
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": format_coin_footer(
                credits_used=credits_used, credits_remaining=credits_remaining,
            )},
        ],
    }


def format_batch_credit_summary(*, credits_used: int, credits_remaining: int) -> str:
    unit = "credit" if credits_used == 1 else "credits"
    return f"{_COIN} This job: {credits_used} {unit} · {credits_remaining} remaining"


def format_dedup_credit_line() -> str:
    return f"{_COIN} No credits used — already in ledger"


def credits_ephemeral_blocks(
    *,
    firm_id: str,
    channel_id: str | None,
    channel_name: str | None = None,
    channel_names: dict[str, str] | None = None,
) -> list[dict]:
    """Ephemeral /ledgr credits card — channel view or master view."""

    balance = read_firm_balance(firm_id)
    by_channel = usage_by_channel(firm_id)
    names = channel_names or {}

    if channel_id and channel_id in by_channel:
        ch_label = channel_name or names.get(channel_id) or channel_id
        ch_used = channel_usage(firm_id, channel_id)
        body = (
            f"*{_COIN} Credits — {ch_label}*\n"
            f"Account balance: *{balance}* credits\n"
            f"This channel (month): *{ch_used}* credits used"
        )
    elif channel_id:
        ch_label = channel_name or names.get(channel_id) or "this channel"
        body = (
            f"*{_COIN} Credits — {ch_label}*\n"
            f"Account balance: *{balance}* credits\n"
            f"This channel (month): *0* credits used"
        )
    else:
        lines = [f"*{_COIN} Ledgr credits*", f"*{balance}* credits remaining", ""]
        if by_channel:
            lines.append("*Usage this month:*")
            for cid, used in sorted(by_channel.items(), key=lambda x: -x[1]):
                label = names.get(cid) or cid
                lines.append(f"  #{label}: {used} {_COIN}")
        else:
            lines.append("_No usage recorded this month yet._")
        body = "\n".join(lines)

    body += (
        f"\n\nLedgr account ID: `{firm_id}`\n"
        "_Quote this when requesting a top-up._"
    )

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Check balance anytime with `/ledgr credits` or open *Ledgr* → *Home*.",
                }
            ],
        },
    ]


def app_home_view(
    *,
    firm_id: str,
    channel_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Home tab view payload for views.publish."""

    balance = read_firm_balance(firm_id)
    by_channel = usage_by_channel(firm_id)
    names = channel_names or {}

    usage_lines: list[str] = []
    if by_channel:
        for cid, used in sorted(by_channel.items(), key=lambda x: -x[1]):
            label = names.get(cid) or cid
            usage_lines.append(f"#{label}: {used} {_COIN}")
    else:
        usage_lines.append("_No usage this month yet._")

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{_COIN} {balance} credits remaining"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Usage this month*\n" + "\n".join(usage_lines),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Ledgr account ID:* `{firm_id}`\n"
                    "Need more credits? Contact your Ledgr operator."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "ledgr_credits_refresh",
                    "text": {"type": "plain_text", "text": "Refresh"},
                },
            ],
        },
    ]
    return {"type": "home", "blocks": blocks}
