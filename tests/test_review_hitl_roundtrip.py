"""Mid-flow extract-review HITL pause→resume roundtrip over the REAL
``review_extraction_node`` + the REAL terminal ``approval_gate``.

Hermetic: no network, no Gemini — ``REVIEWER_FN`` / ``EXTRACT_BUNDLE_FN`` are
faked. Mirrors ``tests/test_hitl_roundtrip.py``. Proves:
- ``review_extraction_node`` yields a ``RequestInput`` whose interrupt id is the
  terminal gate's id suffixed ``:review`` (distinct from the gate's own pause).
- Resuming with ``ReviewClarifyDecision(action="reextract_as", hint=...)``
  re-extracts and continues.
- The SEPARATE terminal ``approval_gate`` STILL fires afterward
  (two-interrupt coexistence).
- ``action="reject"`` empties ``NORMALIZED_KEY``; ``action="confirm_as_is"``
  waves the extraction through.
"""

from __future__ import annotations

import asyncio

from google.adk.apps import App, ResumabilityConfig
from google.adk.artifacts import InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.workflow import START, Workflow
from google.genai import types

from accounting_agents import nodes
from accounting_agents.hitl import build_resume_message
from accounting_agents.nodes import (
    ApproveDecision,
    ReviewClarifyDecision,
    apply_decision_node,
    approval_gate,
    review_extraction_node,
)
from accounting_agents.sessions import FirestoreSessionService
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice
from invoice_processing.extract.invoice_extractor import (
    ExtractedInvoice,
    ExtractedInvoiceBundle,
    ExtractedLine,
)
from tests._fake_firestore import FakeFirestore


def _build_app() -> App:
    # review_extraction_node -> approval_gate -> apply_decision_node: the mid-flow
    # review pause precedes the terminal approval pause (mirrors the real graph),
    # so both interrupts can coexist in one run and the terminal approve decision
    # is applied by apply_decision_node downstream.
    wf = Workflow(
        name="review_wf",
        edges=[(START, review_extraction_node, approval_gate, apply_decision_node)],
    )
    return App(
        name="acc",
        root_agent=wf,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )


def _tripped_state(channel: str) -> dict:
    # An unreconciled invoice trips detect_struggle. A low tax_confidence line
    # ALSO trips the terminal approval_gate, so both pauses can fire.
    inv = NormalizedInvoice(
        invoice_number="INV-LO",
        invoice_date=__import__("datetime").date(2025, 1, 15),
        doc_total=109.0,
        reconciled=False,
        reconcile_note="totals do not reconcile",
        lines=[InvoiceLine(description="ambiguous charge", tax_confidence=0.40)],
    )
    return {
        "op_id": f"{channel}:F1",
        "channel_id": channel,
        "file_id": "F1",
        "direction": "purchase",
        "tax_registered": True,
        nodes.ARTIFACT_NAME_KEY: nodes.ARTIFACT_NAME_FMT.format(file_id="F1"),
        nodes.DOC_TYPE_KEY: "invoice",
        nodes.CLASSIFY_CONFIDENCE_KEY: 0.95,
        nodes.NORMALIZED_KEY: [nodes._inv_to_dict(inv)],
    }


def _fake_clean_bundle(*_a, **_k) -> ExtractedInvoiceBundle:
    """Re-extraction yields a clean, reconciled invoice."""
    return ExtractedInvoiceBundle(
        invoices=[
            ExtractedInvoice(
                doc_type="invoice",
                invoice_number="INV-LO",
                invoice_date="2025-01-15",
                currency="SGD",
                issuer_name="Acme Supplier",
                bill_to_name="Client",
                lines=[ExtractedLine(description="Goods", net_amount=100.0, gst_amount=9.0)],
                subtotal=100.0,
                gst_total=9.0,
                total=109.0,
            )
        ]
    )


def _force_clarify(state, reasons, *, model):
    return {"verdict": nodes.REVIEW_VERDICT_CLARIFY, "question": "Please confirm this document."}


def setup_function(_):
    nodes.REVIEWER_FN = _force_clarify
    nodes.EXTRACT_BUNDLE_FN = _fake_clean_bundle


def teardown_function(_):
    nodes.REVIEWER_FN = nodes._reviewer_llm
    nodes.EXTRACT_BUNDLE_FN = __import__(
        "invoice_processing.extract.invoice_extractor",
        fromlist=["extract_invoice_bundle"],
    ).extract_invoice_bundle


def _new_runner(session_id: str):
    app = _build_app()
    svc = FirestoreSessionService(client=FakeFirestore())
    asyncio.run(
        svc.create_session(
            app_name="acc", user_id=session_id, session_id=session_id,
            state=_tripped_state(session_id),
        )
    )
    # The reextract_as resume path re-reads the source PDF, so the runner needs
    # an artifact service holding a stub PDF under the conventional filename.
    artifacts = InMemoryArtifactService()
    asyncio.run(
        artifacts.save_artifact(
            app_name="acc",
            user_id=session_id,
            session_id=session_id,
            filename=nodes.ARTIFACT_NAME_FMT.format(file_id="F1"),
            artifact=types.Part(
                inline_data=types.Blob(data=b"%PDF-1.4 stub", mime_type="application/pdf")
            ),
        )
    )
    return Runner(app=app, session_service=svc, artifact_service=artifacts), svc


def _collect_interrupt_ids(runner, session_id, message):
    async def drive():
        ids = []
        async for ev in runner.run_async(
            user_id=session_id, session_id=session_id, new_message=message
        ):
            for fc in ev.get_function_calls():
                if fc.name == "adk_request_input":
                    ids.append(fc.id)
        return ids

    return asyncio.run(drive())


def test_review_node_yields_review_interrupt_then_terminal_gate_fires():
    runner, svc = _new_runner("C1")

    # First run: review_extraction_node circuit-breaks to a human → pauses with
    # the ":review" interrupt id.
    ids = _collect_interrupt_ids(
        runner, "C1", types.Content(parts=[types.Part(text="process")])
    )
    assert ids == ["C1:F1:review"]

    # Resume the REVIEW pause with a reextract_as decision → it re-extracts (now
    # CLEAN) and the workflow proceeds PAST the review pause. The terminal
    # approval_gate sees a clean, reconciled invoice and auto-approves (no second
    # pause), so the spine continues to apply_decision_node — proving the review
    # interrupt is distinct from, and resolved independently of, the terminal gate.
    resume_msg = build_resume_message(
        "C1:F1:review",
        ReviewClarifyDecision(action="reextract_as", hint="read the summary"),
    )
    terminal_ids = _collect_interrupt_ids(runner, "C1", resume_msg)
    # The ":review" interrupt is NOT re-raised; the review pause is resolved.
    assert "C1:F1:review" not in terminal_ids

    got = asyncio.run(svc.get_session(app_name="acc", user_id="C1", session_id="C1"))
    assert got.state.get("review_clarify_action") == "reextract_as"
    # The re-extraction repopulated the normalized payload and the spine
    # auto-approved through the SEPARATE terminal gate.
    assert got.state.get(nodes.NORMALIZED_KEY)
    assert got.state.get(nodes.APPROVAL_STATUS_KEY) == "auto_approved"


def test_review_resume_reject_empties_normalized():
    runner, svc = _new_runner("C2")
    ids = _collect_interrupt_ids(
        runner, "C2", types.Content(parts=[types.Part(text="process")])
    )
    assert ids == ["C2:F1:review"]

    resume_msg = build_resume_message("C2:F1:review", ReviewClarifyDecision(action="reject"))

    async def resume():
        async for _ev in runner.run_async(
            user_id="C2", session_id="C2", new_message=resume_msg
        ):
            pass

    asyncio.run(resume())
    got = asyncio.run(svc.get_session(app_name="acc", user_id="C2", session_id="C2"))
    assert got.state.get(nodes.NORMALIZED_KEY) == []
    assert got.state.get("review_clarify_action") == "reject"


def test_review_resume_confirm_as_is_waves_through():
    runner, svc = _new_runner("C3")
    ids = _collect_interrupt_ids(
        runner, "C3", types.Content(parts=[types.Part(text="process")])
    )
    assert ids == ["C3:F1:review"]

    # confirm_as_is keeps the ORIGINAL (still-unreconciled) invoice, which then
    # trips the SEPARATE terminal approval_gate — proving two-interrupt
    # coexistence (the distinct ":review" and terminal ids in one session).
    resume_msg = build_resume_message(
        "C3:F1:review", ReviewClarifyDecision(action="confirm_as_is")
    )
    terminal_ids = _collect_interrupt_ids(runner, "C3", resume_msg)
    assert "C3:F1" in terminal_ids
    assert "C3:F1:review" not in terminal_ids

    got = asyncio.run(svc.get_session(app_name="acc", user_id="C3", session_id="C3"))
    # The original (unchanged) invoice is still present.
    assert got.state.get(nodes.NORMALIZED_KEY)
    assert got.state[nodes.NORMALIZED_KEY][0]["invoice_number"] == "INV-LO"
    assert got.state.get("review_clarify_action") == "confirm_as_is"


def test_terminal_approve_decision_still_resolves_after_review():
    """End-to-end: review confirm_as_is → terminal gate pauses → approve resolves.

    Uses ``confirm_as_is`` so the still-unreconciled invoice trips the terminal
    gate (the reextract path would auto-approve a clean doc), then drives the
    terminal ``ApproveDecision`` to completion.
    """
    runner, svc = _new_runner("C4")
    _collect_interrupt_ids(runner, "C4", types.Content(parts=[types.Part(text="process")]))

    review_resume = build_resume_message(
        "C4:F1:review",
        ReviewClarifyDecision(action="confirm_as_is"),
    )
    terminal_ids = _collect_interrupt_ids(runner, "C4", review_resume)
    assert "C4:F1" in terminal_ids

    # Resume the TERMINAL gate with an approve decision → it resolves.
    approve_resume = build_resume_message("C4:F1", ApproveDecision(decision="approve"))

    async def resume():
        async for _ev in runner.run_async(
            user_id="C4", session_id="C4", new_message=approve_resume
        ):
            pass

    asyncio.run(resume())
    got = asyncio.run(svc.get_session(app_name="acc", user_id="C4", session_id="C4"))
    assert got.state.get(nodes.APPROVAL_STATUS_KEY) == "approve"
