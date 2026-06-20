"""Compatibility probe for new Block Kit primitives.

Posts one Slack message per primitive to a target channel so a human can eyeball
whether each renders or shows the "this content isn't supported" placeholder.

Usage:
    python scripts/smoke_native_blocks.py --channel <CHANNEL_ID_OR_NAME> [--only plan,card,carousel,data_table,context_actions]

Env:
    SLACK_BOT_TOKEN — loaded from .env at repo root (same loader as slack_live_test.py)
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv(".env")

# ---------------------------------------------------------------------------
# Block payloads — sourced from docs.slack.dev, 2026-06-15
# ---------------------------------------------------------------------------

_PLAN_BLOCK: dict = {
    "type": "plan",
    "title": "Processing demo invoice",
    "tasks": [
        {
            "task_id": "t1",
            "title": "Classify",
            "status": "complete",
            "output": {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": "Recognized as invoice"}],
                    }
                ],
            },
        },
        {"task_id": "t2", "title": "Extract", "status": "in_progress"},
        {"task_id": "t3", "title": "Categorize", "status": "pending"},
        {"task_id": "t4", "title": "Tax", "status": "pending"},
        {"task_id": "t5", "title": "Approve", "status": "pending"},
    ],
}

_CARD_BLOCK: dict = {
    "type": "card",
    "icon": {
        "type": "image",
        "image_url": "https://picsum.photos/36/36",
        "alt_text": "icon",
    },
    "title": {"type": "mrkdwn", "text": "Acme Inc"},
    "subtitle": {"type": "mrkdwn", "text": "Invoice #INV-2025-001 · 2025-09-15"},
    "body": {
        "type": "mrkdwn",
        "text": "Total SGD 1,234.50 · SR · Acct 6090 · FY2025 / Sales Ledger",
    },
    "actions": [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Re-extract"},
            "action_id": "smoke_reextract",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Edit"},
            "action_id": "smoke_edit",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "View row"},
            "action_id": "smoke_view",
        },
    ],
}

def _mini_card(n: int) -> dict:
    return {
        "type": "card",
        "title": {"type": "mrkdwn", "text": f"Doc {n}"},
        "body": {"type": "mrkdwn", "text": f"Document {n} summary"},
        "actions": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Open"},
                "action_id": f"smoke_open_{n}",
            }
        ],
    }


_CAROUSEL_BLOCK: dict = {
    "type": "carousel",
    "elements": [_mini_card(1), _mini_card(2), _mini_card(3)],
}

_DATA_TABLE_BLOCK: dict = {
    "type": "data_table",
    "caption": "Recent ledger rows",
    "page_size": 10,
    "row_header_column_index": 0,
    "rows": [
        [
            {"type": "raw_text", "text": "Date"},
            {"type": "raw_text", "text": "Description"},
            {"type": "raw_text", "text": "Account"},
            {"type": "raw_text", "text": "Tax"},
            {"type": "raw_text", "text": "Net"},
            {"type": "raw_text", "text": "Total"},
        ],
        [
            {"type": "raw_text", "text": "2025-09-15"},
            {"type": "raw_text", "text": "Acme Inc INV-2025-001"},
            {"type": "raw_text", "text": "6090"},
            {"type": "raw_text", "text": "SR"},
            {"type": "raw_number", "text": "1132.11", "value": 1132.11},
            {"type": "raw_number", "text": "1234.50", "value": 1234.50},
        ],
        [
            {"type": "raw_text", "text": "2025-09-18"},
            {"type": "raw_text", "text": "BetaCo BIL-7788"},
            {"type": "raw_text", "text": "6090"},
            {"type": "raw_text", "text": "ZR"},
            {"type": "raw_number", "text": "450.00", "value": 450.00},
            {"type": "raw_number", "text": "450.00", "value": 450.00},
        ],
    ],
}

_CONTEXT_ACTIONS_BLOCK: dict = {
    "type": "context_actions",
    "elements": [
        {
            "type": "feedback_buttons",
            "action_id": "smoke_feedback",
            "positive_button": {
                "text": {"type": "plain_text", "text": "👍"},
                "value": "pos",
            },
            "negative_button": {
                "text": {"type": "plain_text", "text": "👎"},
                "value": "neg",
            },
        }
    ],
}

# Ordered registry: (key, label, block_dict)
_ALL_PROBES: list[tuple[str, str, dict]] = [
    ("plan", "plan block", _PLAN_BLOCK),
    ("card", "card block", _CARD_BLOCK),
    ("carousel", "carousel block", _CAROUSEL_BLOCK),
    ("data_table", "data_table block", _DATA_TABLE_BLOCK),
    ("context_actions", "context_actions block", _CONTEXT_ACTIONS_BLOCK),
]

_VALID_KEYS = {k for k, _, _ in _ALL_PROBES}


# ---------------------------------------------------------------------------
# Channel resolution
# ---------------------------------------------------------------------------

def _resolve_channel(client: WebClient, channel_arg: str) -> str:
    """Return a channel ID.

    If the arg already looks like a Slack ID (starts with C or G), return it
    directly. Otherwise resolve by name via conversations.list.
    """
    if channel_arg.startswith(("C", "G")):
        return channel_arg

    name = channel_arg.lstrip("#")
    cursor: str | None = None
    while True:
        kwargs: dict = {"limit": 1000, "exclude_archived": True}
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.conversations_list(**kwargs)
        for ch in resp.get("channels") or []:
            if ch.get("name") == name:
                return ch["id"]
        meta = resp.get("response_metadata") or {}
        cursor = meta.get("next_cursor") or ""
        if not cursor:
            break

    print(f"ERROR: channel '{channel_arg}' not found. "
          "Check the name/ID and that the bot is invited to that channel.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Probe posting
# ---------------------------------------------------------------------------

def _post_probe(client: WebClient, channel_id: str, label: str, probe_block: dict) -> None:
    label_section: dict = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*Probe: {label}*"},
    }
    try:
        client.chat_postMessage(
            channel=channel_id,
            text=f"Probe: {label}",
            blocks=[label_section, probe_block],
        )
        print(f"  OK  {label}")
    except SlackApiError as exc:
        print(
            f"  FAIL  {label}\n"
            f"        error={exc.response.get('error')!r}\n"
            f"        data={exc.response.data}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Post one message per Block Kit primitive to a channel as a render probe.",
    )
    parser.add_argument(
        "--channel",
        required=True,
        metavar="CHANNEL_ID_OR_NAME",
        help="Target channel — Slack ID (C…/G…) or plain name (without #).",
    )
    parser.add_argument(
        "--only",
        metavar="plan,card,carousel,data_table,context_actions",
        default=",".join(k for k, _, _ in _ALL_PROBES),
        help="Comma-separated subset of probes to run (default: all 5).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN is not set. Source .env or export the variable.")
        return 2

    requested = [k.strip() for k in args.only.split(",") if k.strip()]
    unknown = [k for k in requested if k not in _VALID_KEYS]
    if unknown:
        print(f"ERROR: unknown probe key(s): {unknown!r}. Valid: {sorted(_VALID_KEYS)}")
        return 2

    client = WebClient(token=token)

    print(f"Resolving channel: {args.channel!r}")
    channel_id = _resolve_channel(client, args.channel)
    print(f"Posting probes to channel {channel_id} ...")

    probes = [(k, lbl, blk) for k, lbl, blk in _ALL_PROBES if k in requested]
    for i, (_, label, block) in enumerate(probes):
        _post_probe(client, channel_id, label, block)
        if i < len(probes) - 1:
            time.sleep(0.5)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
