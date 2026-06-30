#!/usr/bin/env python3
"""Seed Firestore client profile for a Slack test channel.

Use this once per channel before the first live document upload. Manual alternative:
/ledgr settings in Slack.

Example:
  uv run python scripts/seed_slack_dev_channel.py --channel C0123456789
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Ledgr client profile for a channel")
    parser.add_argument("--channel", required=True, help="Slack channel id (C…)")
    parser.add_argument("--client-id", default="", help="Firestore client id (default: dev-<channel>)")
    parser.add_argument("--client-name", default="Dev Test Client", help="Display name")
    parser.add_argument("--firm-id", default="", help="Billing firm id (default: slack team from API or T_TEST)")
    parser.add_argument("--credits", type=int, default=100, help="Dev credits to grant (in-memory/Firestore store)")
    args = parser.parse_args()

    channel_id = args.channel.strip()
    if not channel_id.startswith("C"):
        print("channel id should look like C0123456789", file=sys.stderr)
        return 2

    client_id = (args.client_id or f"dev-{channel_id.lower()}").strip()

    firm_id = (args.firm_id or os.getenv("LEDGR_DEV_FIRM_ID") or "").strip()
    if not firm_id:
        token = os.getenv("SLACK_BOT_TOKEN", "").strip()
        if token:
            try:
                from slack_sdk import WebClient

                auth = WebClient(token=token).auth_test()
                firm_id = str(auth.get("team_id") or "").strip()
            except Exception as exc:  # noqa: BLE001
                print(f"warn: could not read team_id from Slack ({exc}); using T_TEST")
        if not firm_id:
            firm_id = "T_TEST"

    profile = {
        "client_id": client_id,
        "channel_id": channel_id,
        "client_name": args.client_name,
        "fye_month": 12,
        "accounting_software": "QBS Ledger",
        "gst_registered": True,
        "region": "SINGAPORE",
        "base_currency": "SGD",
        "status": "active",
        "firm_id": firm_id,
        "slack_team_id": firm_id,
        "category_mapping": {},
    }

    from ledgr_slack.client_context import FirestoreClientStore

    store = FirestoreClientStore()
    store.save_profile(profile)
    store.set_channel(channel_id, client_id)

    grants = os.getenv("LEDGR_DEV_CREDIT_GRANTS", "").strip()
    entry = f"{firm_id}:{args.credits}"
    if entry not in grants.split(","):
        os.environ["LEDGR_DEV_CREDIT_GRANTS"] = (
            f"{grants},{entry}".strip(",") if grants else entry
        )

    from ledgr_slack.credit_adapter import wire_shared_credit_service

    wire_shared_credit_service()

    ns = os.getenv("LEDGR_FIRESTORE_NAMESPACE", "").strip()
    prefix = f"{ns}_" if ns else ""
    print("Seeded OK")
    print(f"  channel:     {channel_id}")
    print(f"  client_id:   {client_id}")
    print(f"  firm_id:     {firm_id}")
    print(f"  collections: {prefix}clients, {prefix}channels")
    print(f"  credits:     LEDGR_DEV_CREDIT_GRANTS includes {entry}")
    print("\nNext: invite the bot to the channel, then run  uv run python -m ledgr_slack")
    return 0


if __name__ == "__main__":
    sys.exit(main())
