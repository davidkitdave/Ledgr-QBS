#!/usr/bin/env python3
"""Clear scoped ``seen_doc_keys`` on a bank FY pointer so a re-drop can re-merge.

Use when a workbook was corrupted (empty tabs) but Firestore still blocks
re-append. Does NOT delete workbook rows — only removes matching dedupe keys.

Usage (dry-run):

    python scripts/reset_bank_seen_keys.py \\
        --client-id CLIENT_ID \\
        --fy 2024 \\
        --match Apr

Apply:

    python scripts/reset_bank_seen_keys.py \\
        --client-id CLIENT_ID \\
        --fy 2024 \\
        --match Apr \\
        --apply

``--match`` is a case-insensitive substring matched against each ``doc_key``.
Pass multiple ``--match`` flags to require ALL substrings (AND).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from google.cloud import firestore  # noqa: E402

from ledgr_slack.ledger_store import SlackLedgerStore  # noqa: E402

logger = logging.getLogger(__name__)


def _keys_matching(seen: list[str], needles: list[str]) -> list[str]:
    out: list[str] = []
    for key in seen:
        lower = key.lower()
        if all(n.lower() in lower for n in needles):
            out.append(key)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--fy", required=True, help="Financial year label, e.g. 2024")
    parser.add_argument(
        "--match",
        action="append",
        default=[],
        help="Substring that must appear in doc_key (repeatable, ANDed)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes; default is dry-run only",
    )
    args = parser.parse_args()

    if not args.match:
        parser.error("provide at least one --match substring")

    db = firestore.Client()
    store = SlackLedgerStore(db)
    pointer = store.get_pointer(args.client_id, args.fy, kind="bank")
    if not pointer:
        print(f"No bank pointer for client={args.client_id} fy={args.fy}")
        return 1

    seen = list(pointer.get("seen_doc_keys") or [])
    targets = _keys_matching(seen, args.match)
    if not targets:
        print("No matching seen_doc_keys — nothing to do.")
        return 0

    print(f"Pointer file: {pointer.get('slack_file_id')}")
    print(f"Will purge {len(targets)} key(s):")
    for k in targets:
        print(f"  - {k}")

    if not args.apply:
        print("\nDry-run — pass --apply to purge keys.")
        return 0

    purged = store.purge_seen_doc_keys(
        args.client_id, args.fy, targets, kind="bank"
    )
    print(f"\nPurged {purged} key(s). Re-drop the bank PDF to re-merge.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
