"""HITL pause→resume roundtrip over the real ``approval_gate`` + persistent store.

Hermetic: no network, no Gemini. A small resumable workflow runs the real
``approval_gate`` node (low-confidence input → it yields ``RequestInput`` and
pauses); we feed an "approve" decision via the verified resume payload and assert
the workflow continues to the downstream node exactly once.
"""

from __future__ import annotations

import asyncio

from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.workflow import START, Workflow, node
from google.genai import types

from accounting_agents import nodes
from accounting_agents.hitl import build_resume_message, resume_session, write_interrupt
from accounting_agents.nodes import ApproveDecision, approval_gate
from accounting_agents.sessions import FirestoreSessionService
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice
from tests._fake_firestore import FakeFirestore

DOWNSTREAM_RUNS: list = []


@node
async def downstream(ctx, node_input) -> Event:
    """Successor of the gate; records that it ran (and the resumed decision)."""
    DOWNSTREAM_RUNS.append(node_input)
    ctx.state["reached_downstream"] = True
    return Event(output={"ok": True})


def _build_app() -> App:
    wf = Workflow(name="gate_wf", edges=[(START, approval_gate, downstream)])
    return App(
        name="acc",
        root_agent=wf,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )


def _low_confidence_state() -> dict:
    inv = NormalizedInvoice(
        invoice_number="INV-LO",
        reconciled=True,
        lines=[InvoiceLine(description="ambiguous charge", tax_confidence=0.40)],
    )
    return {
        "op_id": "C1:F1",
        "channel_id": "C1",
        "file_id": "F1",
        nodes.NORMALIZED_KEY: [nodes._inv_to_dict(inv)],
    }


def _high_confidence_state() -> dict:
    inv = NormalizedInvoice(
        invoice_number="INV-OK",
        reconciled=True,
        lines=[InvoiceLine(description="clear charge", tax_confidence=0.99)],
    )
    return {nodes.NORMALIZED_KEY: [nodes._inv_to_dict(inv)]}


def setup_function(_):
    DOWNSTREAM_RUNS.clear()


def test_low_confidence_pauses_then_resumes_once():
    app = _build_app()
    svc = FirestoreSessionService(client=FakeFirestore())
    asyncio.run(
        svc.create_session(
            app_name="acc", user_id="C1", session_id="C1", state=_low_confidence_state()
        )
    )
    runner = Runner(app=app, session_service=svc)

    async def drive():
        interrupt_id = None
        async for ev in runner.run_async(
            user_id="C1",
            session_id="C1",
            new_message=types.Content(parts=[types.Part(text="process")]),
        ):
            for fc in ev.get_function_calls():
                if fc.name == "adk_request_input":
                    interrupt_id = fc.id
        return interrupt_id

    interrupt_id = asyncio.run(drive())
    # The gate paused: a RequestInput interrupt was emitted, downstream did NOT run.
    assert interrupt_id == "C1:F1"
    assert DOWNSTREAM_RUNS == []

    # Resume with an approval decision via the verified FunctionResponse payload.
    resume_msg = build_resume_message("C1:F1", ApproveDecision(decision="approve"))

    async def resume():
        async for _ev in runner.run_async(
            user_id="C1", session_id="C1", new_message=resume_msg
        ):
            pass

    asyncio.run(resume())

    # Downstream ran exactly once and received the decision as its node_input.
    assert len(DOWNSTREAM_RUNS) == 1
    assert DOWNSTREAM_RUNS[0]["decision"] == "approve"
    got = asyncio.run(svc.get_session(app_name="acc", user_id="C1", session_id="C1"))
    assert got.state.get("reached_downstream") is True


def test_high_confidence_passes_through_without_pause():
    app = _build_app()
    svc = FirestoreSessionService(client=FakeFirestore())
    asyncio.run(
        svc.create_session(
            app_name="acc", user_id="C2", session_id="C2", state=_high_confidence_state()
        )
    )
    runner = Runner(app=app, session_service=svc)

    async def drive():
        interrupts = []
        async for ev in runner.run_async(
            user_id="C2",
            session_id="C2",
            new_message=types.Content(parts=[types.Part(text="process")]),
        ):
            for fc in ev.get_function_calls():
                if fc.name == "adk_request_input":
                    interrupts.append(fc.id)
        return interrupts

    interrupts = asyncio.run(drive())
    # No pause: gate auto-approved and the workflow reached downstream in one run.
    assert interrupts == []
    assert len(DOWNSTREAM_RUNS) == 1
    got = asyncio.run(svc.get_session(app_name="acc", user_id="C2", session_id="C2"))
    assert got.state.get("approval_status") == "auto_approved"
    assert got.state.get("reached_downstream") is True


def test_resume_session_helper_drives_runner():
    """The hitl.resume_session helper resumes using the Firestore interrupt doc."""
    app = _build_app()
    db = FakeFirestore()
    svc = FirestoreSessionService(client=db)
    asyncio.run(
        svc.create_session(
            app_name="acc", user_id="C3", session_id="C3", state=_low_confidence_state_for("C3")
        )
    )
    runner = Runner(app=app, session_service=svc)

    # Pause.
    async def drive():
        async for _ev in runner.run_async(
            user_id="C3",
            session_id="C3",
            new_message=types.Content(parts=[types.Part(text="process")]),
        ):
            pass

    asyncio.run(drive())
    assert DOWNSTREAM_RUNS == []

    # Slack layer would have written this correlation doc when it posted the card.
    write_interrupt(
        db,
        "C3:F3",
        session_id="C3",
        channel_id="C3",
        slack_file_id="F3",
        message_ts="123.456",
    )

    events = asyncio.run(
        resume_session(runner, db, "C3:F3", ApproveDecision(decision="approve"))
    )
    assert events  # resume produced events
    assert len(DOWNSTREAM_RUNS) == 1


def _low_confidence_state_for(channel: str) -> dict:
    inv = NormalizedInvoice(
        invoice_number="INV-LO",
        reconciled=True,
        lines=[InvoiceLine(description="ambiguous charge", tax_confidence=0.40)],
    )
    return {
        "op_id": f"{channel}:F3",
        "channel_id": channel,
        "file_id": "F3",
        nodes.NORMALIZED_KEY: [nodes._inv_to_dict(inv)],
    }
