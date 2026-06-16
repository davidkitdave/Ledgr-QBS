#!/usr/bin/env python3
"""Upload a PDF to Slack and run the Ledgr pipeline without waiting for socket events.

Slack does not deliver ``message`` events for a bot's own posts, so ``files.upload``
from the bot token will not trigger the socket-mode handler. This script uploads
then calls :func:`process_file_event` directly — review cards still land in-channel.
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

from accounting_agents.ledger_store import SlackLedgerStore
from accounting_agents.sessions import FirestoreSessionService
from accounting_agents.slack_runner import (
    build_runner,
    download_pdf_bytes,
    handle_approval_action,
    process_file_event,
)
from invoice_processing.export.client_context import FirestoreClientStore


async def upload_and_process(
    pdf: Path,
    *,
    channel_id: str,
    comment: str = "",
    auto_approve: bool = False,
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

    result = await process_file_event(
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

    if auto_approve and result.get("status") == "paused":
        op_id = result.get("op_id") or ""
        if op_id.endswith(":review"):
            print(f"Paused at extract review ({op_id}); auto-approve skipped.", file=sys.stderr)
            return result
        approve_result = await handle_approval_action(
            runner=runner,
            ledger_store=ledger_store,
            db=db,
            slack_client=client,
            op_id=op_id,
            decision="approve",
            app_name=runner.app_name,
        )
        return {"upload": result, "approve": approve_result}

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload PDF to Slack and process via Ledgr")
    parser.add_argument("pdf", type=Path, help="Path to PDF")
    parser.add_argument(
        "--channel",
        default=os.environ.get("LEDGR_TEST_CHANNEL", "C0123456789"),
        help="Slack channel id (default: #acme-client-test)",
    )
    parser.add_argument("--comment", default="", help="Optional upload comment")
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Auto-approve at the HITL gate (writes Excel + posts data_table preview)",
    )
    args = parser.parse_args()

    result = asyncio.run(
        upload_and_process(
            args.pdf,
            channel_id=args.channel,
            comment=args.comment,
            auto_approve=args.approve,
        )
    )
    print(result)


if __name__ == "__main__":
    main()
