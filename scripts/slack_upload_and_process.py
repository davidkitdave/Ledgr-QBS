#!/usr/bin/env python3
"""Upload a PDF to Slack and run the Ledgr pipeline without waiting for socket events.

Slack does not deliver ``message`` events for a bot's own posts, so ``files.upload``
from the bot token will not trigger the socket-mode handler. This script uploads
then calls :func:`process_file_event` directly — delivery lands in-channel.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")

from slack_sdk import WebClient

from ledgr_slack.ledger_store import SlackLedgerStore
from ledgr_slack.sessions import FirestoreSessionService
from ledgr_slack.app import (
    build_runner,
    download_pdf_bytes,
    process_file_event,
)
from ledgr_slack.client_context import FirestoreClientStore


async def upload_and_process(
    pdf: Path,
    *,
    channel_id: str,
    comment: str = "",
) -> dict:
    pdf = pdf.expanduser()
    if not pdf.exists():
        raise FileNotFoundError(pdf)

    token = os.environ["SLACK_BOT_TOKEN"]
    client = WebClient(token=token)

    resp = client.files_upload_v2(
        channel=channel_id,
        file=str(pdf),
        title=pdf.name,
        initial_comment=comment or f"[dev] processing {pdf.name}",
    )
    file_id = (resp.get("file") or {}).get("id")
    if not file_id:
        raise RuntimeError(f"files.upload failed: {resp}")

    db = FirestoreSessionService().client
    runner = build_runner()
    ledger_store = SlackLedgerStore(db)

    return await process_file_event(
        runner=runner,
        ledger_store=ledger_store,
        db=db,
        slack_client=client,
        channel_id=channel_id,
        file_id=file_id,
        app_name=runner.app_name,
        download_fn=download_pdf_bytes,
        source_filename=pdf.name,
        client_store=FirestoreClientStore(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload PDF to Slack and process via Ledgr")
    parser.add_argument("pdf", type=Path, help="Path to PDF")
    parser.add_argument(
        "--channel",
        default=os.environ.get("LEDGR_TEST_CHANNEL", "C0123456789"),
        help="Slack channel id (default: #acme-client-test)",
    )
    parser.add_argument("--comment", default="", help="Optional upload comment")
    args = parser.parse_args()

    result = asyncio.run(
        upload_and_process(
            args.pdf,
            channel_id=args.channel,
            comment=args.comment,
        )
    )
    print(result)


if __name__ == "__main__":
    main()
