"""Playground runner — test ADK agents locally without Slack.

Usage::

    # Document lane: process a PDF end-to-end, output to local Excel
    uv run python -m accounting_agents.playground_runner \\
        --pdf tests/eval_invoices/sample_invoice.pdf \\
        --client-id playground --software qbs

    # Chat lane: interactive chat with ledger data loaded from local store
    uv run python -m accounting_agents.playground_runner \\
        --chat --client-id playground

    # List what's in the local store
    uv run python -m accounting_agents.playground_runner \\
        --list --client-id playground

Design: this module does what ``slack_runner.py`` does, minus all Slack I/O:
1. Seeds session state (client profile, ledger data)
2. Drives the ADK ``Runner`` on ``document_app`` or ``assistant_app``
3. Reads ``state["ledger_rows"]`` and calls ``LocalLedgerStore.append_rows``
4. Prints the delivery summary / chat response
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from accounting_agents.local_ledger_store import LocalLedgerStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT_DIR = "./playground_output"


def _build_client_state(
    *,
    client_id: str = "playground",
    client_name: str = "Playground Client",
    region: str = "SINGAPORE",
    software: str = "qbs",
    base_currency: str = "SGD",
    tax_registered: bool = True,
    fye_month: int = 12,
) -> dict:
    """Build the state dict that ``slack_runner._profile_state_delta`` returns."""
    return {
        "client_id": client_id,
        "client_name": client_name,
        "region": region,
        "software": software,
        "base_currency": base_currency,
        "tax_registered": tax_registered,
        "fye_month": fye_month,
        "channel_id": f"playground-{client_id}",
        "coa": [],
        "category_mapping": {},
        "entity_memory": [],
    }


async def run_document(pdf_path: str, client_state: dict, output_dir: str) -> None:
    """Run the document lane: PDF → extract → categorize → tax → local Excel."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from accounting_agents.agent import document_app
    from accounting_agents import nodes

    pdf_bytes = Path(pdf_path).read_bytes()
    mime_type = "application/pdf"
    ext = Path(pdf_path).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        mime_type = f"image/{ext.lstrip('.')}"
        if ext in (".jpg", ".jpeg"):
            mime_type = "image/jpeg"

    from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService

    session_service = InMemorySessionService()
    artifact_service = InMemoryArtifactService()
    runner = Runner(
        app_name=document_app.name,
        agent=document_app.root_agent,
        session_service=session_service,
        artifact_service=artifact_service,
    )

    user_id = client_state.get("channel_id", "playground")
    session_id = f"{user_id}:doc:playground"
    await session_service.create_session(
        app_name=document_app.name,
        user_id=user_id,
        session_id=session_id,
        state=client_state,
    )

    # Seed the PDF as inline_data (the playground fallback path in _load_pdf_bytes).
    user_message = types.Content(
        role="user",
        parts=[
            types.Part(text="Process this document."),
            types.Part(inline_data=types.Blob(mime_type=mime_type, data=pdf_bytes)),
        ],
    )

    print(f"\n📄 Processing: {pdf_path}")
    print(f"   Client: {client_state['client_name']} ({client_state['client_id']})")
    print(f"   Software: {client_state['software']}")
    print("=" * 60)

    final_text = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=user_message,
    ):
        # Show progress.
        node_name = _event_node_name(event)
        if node_name:
            print(f"   ⚙️  {node_name}")

        # Capture final text.
        text = _extract_text(event)
        if text:
            final_text = text

        # Check for HITL interrupt.
        if _is_interrupt(event):
            print("\n🛑 HITL interrupt — the agent wants human input.")
            print("   In the ADK playground, you'd see the approval card here.")
            print("   For now, the flow pauses here. Use --auto-approve to skip.\n")

    # Read state after run.
    session = await session_service.get_session(
        app_name=document_app.name,
        user_id=user_id,
        session_id=session_id,
    )
    state = dict(session.state) if session else {}

    # Persist to local Excel.
    payload = state.get(nodes.LEDGER_ROWS_KEY) or {}
    batches = payload.get("batches") or []
    if batches:
        store = LocalLedgerStore(output_dir=output_dir)
        result = store.append_rows(
            client_id=client_state["client_id"],
            fy=payload.get("fy", "2026"),
            batches=batches,
            software=payload.get("software", "qbs"),
            kind=payload.get("kind", "invoice"),
            client_name=client_state["client_name"],
        )
        print(f"\n✅ Excel written: {result['workbook_path']}")
        print(f"   Rows appended: {result['appended']}")
        print(f"   Deduped: {result['deduped']}")
    else:
        print("\n⚠️  No ledger rows produced — check the extraction output.")
        # Dump relevant state keys for debugging.
        debug_keys = [
            "doc_type", "direction", "normalized", "classify_result",
            "approval_status", "review_verdict", "review_reasons",
        ]
        for key in debug_keys:
            val = state.get(key)
            if val is not None:
                val_str = json.dumps(val, default=str)
                if len(val_str) > 300:
                    val_str = val_str[:300] + "..."
                print(f"   state[{key!r}] = {val_str}")

    if final_text:
        print(f"\n📝 Agent summary: {final_text[:500]}")


async def run_chat(client_state: dict, output_dir: str) -> None:
    """Run the chat lane interactively with locally-stored ledger data."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from accounting_agents.agent import assistant_app

    # Load ledger data from local store.
    store = LocalLedgerStore(output_dir=output_dir)
    client_id = client_state["client_id"]
    latest_fy = store.latest_fy(client_id)
    ledger_rows = store.read_rows(client_id, latest_fy) if latest_fy else []
    fy_pointers = store.fy_pointers(client_id)

    # Seed chat state.
    chat_state = dict(client_state)
    chat_state["ledger_data"] = ledger_rows
    chat_state["ledger_row_count"] = len(ledger_rows)
    chat_state["fy_loaded"] = latest_fy or "none"
    chat_state["processing_log"] = []
    chat_state["pending_reviews"] = []
    chat_state["fy_pointers"] = fy_pointers

    from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService

    session_service = InMemorySessionService()
    artifact_service = InMemoryArtifactService()
    runner = Runner(
        app_name=assistant_app.name,
        agent=assistant_app.root_agent,
        session_service=session_service,
        artifact_service=artifact_service,
    )

    user_id = client_state.get("channel_id", "playground")
    session_id = f"{user_id}:chat:playground"
    await session_service.create_session(
        app_name=assistant_app.name,
        user_id=user_id,
        session_id=session_id,
        state=chat_state,
    )

    print(f"\n💬 Ledgr Chat Assistant (Playground)")
    print(f"   Client: {client_state['client_name']}")
    print(f"   Loaded: FY{latest_fy or 'none'} ({len(ledger_rows)} rows)")
    print(f"   Type 'quit' to exit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not user_input or user_input.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break

        user_message = types.Content(
            role="user",
            parts=[types.Part(text=user_input)],
        )

        response_text = ""
        tool_calls = []
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=user_message,
        ):
            text = _extract_text(event)
            if text:
                response_text = text

            # Track tool calls for debugging.
            fn_calls = _extract_tool_calls(event)
            if fn_calls:
                tool_calls.extend(fn_calls)

        if tool_calls:
            print(f"   🔧 Tools called: {', '.join(tool_calls)}")
        if response_text:
            print(f"\n🤖 {response_text}\n")
        else:
            print("\n🤖 (no text response — model may have only called tools)\n")


def list_store(client_id: str, output_dir: str) -> None:
    """List what's in the local ledger store."""
    store = LocalLedgerStore(output_dir=output_dir)
    pointers = store.fy_pointers(client_id)
    if not pointers:
        print(f"\nNo workbooks found for client '{client_id}' in {output_dir}/")
        print("Process a PDF first: --pdf <path>")
        return
    print(f"\n📂 Local ledger store for '{client_id}':")
    for p in pointers:
        status = "✅" if p.get("has_data") else "⚪"
        print(f"   {status} FY{p['fy']}: {p.get('row_count', 0)} rows "
              f"({p.get('kind', '?')}) — {p.get('workbook_path', '?')}")


# --------------------------------------------------------------------------- #
# Event inspection helpers (simplified from slack_runner)
# --------------------------------------------------------------------------- #


def _event_node_name(event) -> str | None:
    node_info = getattr(event, "node_info", None)
    path = getattr(node_info, "path", None) if node_info else None
    if not path:
        return None
    last = str(path).rsplit("/", 1)[-1]
    return last.split("@", 1)[0] or None


def _extract_text(event) -> str:
    content = getattr(event, "content", None)
    if content is None:
        return ""
    parts = getattr(content, "parts", None) or []
    return "".join(p.text for p in parts if getattr(p, "text", None))


def _extract_tool_calls(event) -> list[str]:
    getter = getattr(event, "get_function_calls", None)
    calls = getter() if callable(getter) else []
    return [
        getattr(fc, "name", "?")
        for fc in (calls or [])
        if getattr(fc, "name", None) not in (None, "adk_request_input")
    ]


def _is_interrupt(event) -> bool:
    getter = getattr(event, "get_function_calls", None)
    calls = getter() if callable(getter) else []
    for fc in calls or []:
        if getattr(fc, "name", None) == "adk_request_input":
            return True
    return False


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main():
    parser = argparse.ArgumentParser(
        description="Ledgr ADK Playground Runner — test agents locally without Slack.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Process a PDF end-to-end → outputs to ./playground_output/
  uv run python -m accounting_agents.playground_runner \\
      --pdf tests/eval_invoices/sample_invoice.pdf

  # Interactive chat (reads ledger data from local store)
  uv run python -m accounting_agents.playground_runner --chat

  # List what's been processed
  uv run python -m accounting_agents.playground_runner --list
""",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pdf", metavar="PATH", help="Process a PDF through the document lane")
    mode.add_argument("--chat", action="store_true", help="Interactive chat with ledger data")
    mode.add_argument("--list", action="store_true", help="List local store contents")

    parser.add_argument("--client-id", default="playground", help="Client ID (default: playground)")
    parser.add_argument("--client-name", default="Playground Client", help="Client display name")
    parser.add_argument("--software", default="qbs", choices=["qbs", "xero"], help="Accounting software")
    parser.add_argument("--region", default="SINGAPORE", help="Region (SINGAPORE/MALAYSIA)")
    parser.add_argument("--currency", default="SGD", help="Base currency")
    parser.add_argument("--fye-month", type=int, default=12, help="Financial year end month")
    parser.add_argument("--no-gst", action="store_true", help="Client is NOT GST-registered")
    parser.add_argument("--output-dir", default=_DEFAULT_OUTPUT_DIR, help="Local store directory")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    client_state = _build_client_state(
        client_id=args.client_id,
        client_name=args.client_name,
        region=args.region,
        software=args.software,
        base_currency=args.currency,
        tax_registered=not args.no_gst,
        fye_month=args.fye_month,
    )

    if args.list:
        list_store(args.client_id, args.output_dir)
    elif args.pdf:
        if not Path(args.pdf).exists():
            print(f"❌ File not found: {args.pdf}", file=sys.stderr)
            sys.exit(1)
        asyncio.run(run_document(args.pdf, client_state, args.output_dir))
    elif args.chat:
        asyncio.run(run_chat(client_state, args.output_dir))


if __name__ == "__main__":
    main()
