"""Resume idempotency: a paused workflow survives a restart and resumes once.

Hermetic. Proves the two production guarantees:

1. Cross-restart resume — run to the HITL gate, DROP the runner, recreate a
   brand-new runner from the persisted Firestore session, and resume. The
   downstream effect happens (state persisted entirely in Firestore, no live
   in-memory runner state required).
2. Double-resume is a no-op — calling ``resume_session`` twice for the same
   ``op_id`` runs the downstream node exactly once (zero duplicates) thanks to
   the ``processed/{op_id}`` marker.
"""

from __future__ import annotations

import asyncio

from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.workflow import START, Workflow, node
from google.genai import types

from accounting_agents import nodes
from accounting_agents.hitl import is_processed, resume_session, write_interrupt
from accounting_agents.nodes import ApproveDecision, approval_gate
from accounting_agents.sessions import FirestoreSessionService
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice
from tests._fake_firestore import FakeFirestore

DELIVER_CALLS: list = []


@node
async def deliver(ctx, node_input) -> Event:
    """Terminal node standing in for consolidate/deliver — must run ONCE."""
    DELIVER_CALLS.append(node_input)
    ctx.state["delivered_count"] = ctx.state.get("delivered_count", 0) + 1
    return Event(output={"delivered": True})


def _build_app() -> App:
    wf = Workflow(name="idem_wf", edges=[(START, approval_gate, deliver)])
    return App(
        name="acc",
        root_agent=wf,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )


def _low_confidence_state() -> dict:
    inv = NormalizedInvoice(
        invoice_number="INV-LO",
        reconciled=False,  # unreconciled -> needs review
        reconcile_note="totals off by 0.05",
        lines=[InvoiceLine(description="charge", tax_confidence=0.5)],
    )
    return {
        "op_id": "C9:F9",
        "channel_id": "C9",
        "file_id": "F9",
        nodes.NORMALIZED_KEY: [nodes._inv_to_dict(inv)],
    }


def setup_function(_):
    DELIVER_CALLS.clear()


def _pause(db) -> FirestoreSessionService:
    """Run the workflow up to the HITL pause; return the (persistent) service."""
    app = _build_app()
    svc = FirestoreSessionService(client=db)
    asyncio.run(
        svc.create_session(
            app_name="acc", user_id="C9", session_id="C9", state=_low_confidence_state()
        )
    )
    runner = Runner(app=app, session_service=svc)

    async def drive():
        async for _ev in runner.run_async(
            user_id="C9",
            session_id="C9",
            new_message=types.Content(parts=[types.Part(text="process")]),
        ):
            pass

    asyncio.run(drive())
    assert DELIVER_CALLS == []  # paused before delivery
    # Slack layer's correlation doc.
    write_interrupt(
        db, "C9:F9", session_id="C9", channel_id="C9", slack_file_id="F9"
    )
    return svc


def test_resume_after_runner_dropped_and_recreated():
    db = FakeFirestore()
    _pause(db)  # run to the HITL pause; discard the original service instance

    # Simulate a bot restart: a brand-new runner + brand-new service instance,
    # both reading the SAME Firestore backing store.
    app2 = _build_app()
    svc2 = FirestoreSessionService(client=db)
    runner2 = Runner(app=app2, session_service=svc2)

    events = asyncio.run(
        resume_session(runner2, db, "C9:F9", ApproveDecision(decision="approve"))
    )
    assert events
    assert len(DELIVER_CALLS) == 1

    got = asyncio.run(svc2.get_session(app_name="acc", user_id="C9", session_id="C9"))
    assert got.state.get("delivered_count") == 1


def test_double_resume_is_noop():
    db = FakeFirestore()
    _pause(db)  # run to the HITL pause and write the correlation doc

    app2 = _build_app()
    runner2 = Runner(app=app2, session_service=FirestoreSessionService(client=db))

    first = asyncio.run(
        resume_session(runner2, db, "C9:F9", ApproveDecision(decision="approve"))
    )
    assert first
    assert is_processed(db, "C9:F9") is True

    # Second resume (e.g. a double-click) must be a no-op: zero new deliveries.
    second = asyncio.run(
        resume_session(runner2, db, "C9:F9", ApproveDecision(decision="approve"))
    )
    assert second == []
    assert len(DELIVER_CALLS) == 1  # exactly once, no duplicates
