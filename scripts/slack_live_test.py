"""Live Slack smoke driver — verifies upload→ledger in a real workspace.

Prereqs:
  1. A Slack app created from `slack/manifest.json`, installed to your workspace.
  2. `.env` has SLACK_BOT_TOKEN (xoxb-…), SLACK_SIGNING_SECRET, SLACK_APP_TOKEN (xapp-…).
  3. The bot is RUNNING (in another terminal):  uv run python -m python -m accounting_agents.slack_runner
  4. The bot is invited to a test channel; set its id in LEDGR_TEST_CHANNEL.
  5. (First time in that channel) run /ledgr settings and upload a COA xlsx/csv,
     so the client is active.

Then:  LEDGR_TEST_CHANNEL=C0123 uv run python scripts/slack_live_test.py

It uploads a sample bill (as an accountant would), then polls the channel for the
bot's .xlsx ledger reply and prints LIVE_OK / LIVE_TIMEOUT.
"""
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(".env")

from slack_sdk import WebClient


def main() -> int:
    token = os.getenv("SLACK_BOT_TOKEN")
    channel = os.getenv("LEDGR_TEST_CHANNEL")
    pdf = os.getenv("LEDGR_TEST_PDF", "")
    timeout_s = int(os.getenv("LEDGR_TEST_TIMEOUT", "150"))

    if not token or not channel or not pdf:
        print(
            "Set SLACK_BOT_TOKEN (.env), LEDGR_TEST_CHANNEL, and LEDGR_TEST_PDF (path to a "
            "sample bill PDF), and make sure the bot is running:  uv run python -m python -m accounting_agents.slack_runner"
        )
        return 2

    client = WebClient(token=token)

    print(f"→ uploading test bill to channel {channel} …")
    up = client.files_upload_v2(
        channel=channel, file=pdf, filename="ledgr_live_test_bill.pdf",
        title="Ledgr live test bill",
    )
    my_file_id = up["file"]["id"]
    print(f"  uploaded file id={my_file_id}; waiting for the bot's ledger reply …")

    deadline = time.time() + timeout_s
    found = None
    while time.time() < deadline:
        hist = client.conversations_history(channel=channel, limit=15)
        for msg in hist["messages"]:
            for f in msg.get("files", []):
                name = f.get("name", "")
                if name.endswith(".xlsx") and f["id"] != my_file_id:
                    found = name
                    break
            if found:
                break
        if found:
            break
        time.sleep(5)

    if found:
        print(f"✅ LIVE_OK — bot returned a ledger workbook: {found}")
        return 0
    print(
        "⏱️ LIVE_TIMEOUT — no .xlsx reply within "
        f"{timeout_s}s. Check: bot running (python -m accounting_agents.slack_runner), bot invited to the channel, "
        "and the client is set up + active (/ledgr settings + COA)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
