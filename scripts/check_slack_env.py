#!/usr/bin/env python3
"""Check that .env has what a live Slack smoke test needs (no secrets printed)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

REQUIRED = [
    ("SLACK_BOT_TOKEN", "xoxb-… bot token"),
    ("SLACK_APP_TOKEN", "xapp-… socket-mode token"),
    ("SLACK_SIGNING_SECRET", "signing secret"),
    ("GOOGLE_API_KEY", "Gemini AI Studio key (read_doc)"),
]

OPTIONAL = [
    ("LEDGR_TEST_CHANNEL", "Slack channel id for smoke scripts"),
    ("LEDGR_FIRESTORE_NAMESPACE", "dev Firestore prefix (recommended: dev)"),
    ("GOOGLE_CLOUD_PROJECT", "GCP project for Firestore"),
    ("GOOGLE_APPLICATION_CREDENTIALS", "service account json path"),
    ("LEDGR_DEV_CREDIT_GRANTS", "e.g. T01234:100 for test credits"),
    ("LEDGR_CREDIT_REQUIRE_FIRM", "1 blocks uploads when slack_team_id missing"),
    ("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8090 if using emulator"),
]


def _ok(name: str) -> bool:
    return bool((os.getenv(name) or "").strip())


def main() -> int:
    print("Ledgr live Slack env check\n")
    missing = [name for name, _ in REQUIRED if not _ok(name)]
    for name, hint in REQUIRED:
        status = "OK" if _ok(name) else "MISSING"
        print(f"  [{status:7}] {name} — {hint}")

    print("\nOptional:")
    for name, hint in OPTIONAL:
        val = (os.getenv(name) or "").strip()
        if val:
            print(f"  [set    ] {name} — {hint}")
        else:
            print(f"  [unset  ] {name} — {hint}")

    firestore = _ok("FIRESTORE_EMULATOR_HOST") or _ok("GOOGLE_APPLICATION_CREDENTIALS")
    if not firestore and _ok("GOOGLE_CLOUD_PROJECT"):
        print(
            "\nNote: GOOGLE_CLOUD_PROJECT is set but no Firestore creds/emulator. "
            "Socket mode needs Firestore for client profiles + sessions."
        )

    if missing:
        print(f"\nFix {len(missing)} required var(s) in .env, then re-run.")
        return 1

    print("\nRequired vars look good. Next:")
    print("  1. uv run python scripts/seed_slack_dev_channel.py --channel YOUR_CHANNEL_ID")
    print("  2. uv run python -m ledgr_slack   # terminal 1 — keep running")
    print("  3. Upload a PDF in Slack, or:")
    print("     LEDGR_TEST_CHANNEL=C… uv run python scripts/slack_upload_and_process.py \\")
    print("       ledgr_agent/eval/fixtures/pdfs/receipt_single.pdf")
    return 0


if __name__ == "__main__":
    sys.exit(main())
