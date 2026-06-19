"""Read-only QA diagnostic: run one PDF through document_app and dump full pre-gate state.

Mirrors playground_runner.run_document but dumps ALL state keys (esp. tax/jurisdiction/
currency/per-line treatment) so we can QA the reasoning even though the HITL gate blocks booking.

Usage:
    uv run python scratch/inspect_state.py <pdf_path> <region> <currency> <client_name>
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


async def main(pdf_path, region, currency, client_name):
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
    from google.genai import types

    from accounting_agents.agent import document_app

    client_state = {
        "client_id": "qa-inspect",
        "client_name": client_name,
        "region": region,
        "software": "qbs",
        "base_currency": currency,
        "tax_registered": True,
        "fye_month": 12,
        "channel_id": "qa-inspect",
        "coa": [],
        "category_mapping": {},
        "entity_memory": [],
    }

    pdf_bytes = Path(pdf_path).read_bytes()
    session_service = InMemorySessionService()
    runner = Runner(
        app_name=document_app.name,
        agent=document_app.root_agent,
        session_service=session_service,
        artifact_service=InMemoryArtifactService(),
    )
    user_id = "qa-inspect"
    session_id = "qa-inspect:doc"
    await session_service.create_session(
        app_name=document_app.name, user_id=user_id, session_id=session_id, state=client_state,
    )
    msg = types.Content(role="user", parts=[
        types.Part(text="Process this document."),
        types.Part(inline_data=types.Blob(mime_type="application/pdf", data=pdf_bytes)),
    ])
    async for _ in runner.run_async(user_id=user_id, session_id=session_id, new_message=msg):
        pass

    session = await session_service.get_session(
        app_name=document_app.name, user_id=user_id, session_id=session_id,
    )
    state = dict(session.state) if session else {}

    print("\n" + "=" * 70)
    print("FULL POST-RUN STATE (pre-booking, paused at HITL gate)")
    print("=" * 70)
    # Highlight the keys we care about for the tax/jurisdiction edge first.
    priority = [
        "region", "base_currency", "doc_type", "direction", "tax_jurisdiction",
        "jurisdiction_rates", "review_verdict", "review_reasons", "approval_status",
        "tax_flagged", "tax_flags", "normalized", "normalized_invoices",
        "booking_proposal", "ledger_rows", "tax_result", "categorize_result",
    ]
    seen = set()
    for key in priority:
        if key in state:
            seen.add(key)
            v = json.dumps(state[key], default=str)
            print(f"\n### {key} ###\n{v[:1500]}")
    print("\n--- other state keys present ---")
    print([k for k in state.keys() if k not in seen])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]))
