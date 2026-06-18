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


def _fake_clean_bundle(*_a, **_k):
    from tests.test_nodes import _legacy_result_from_ex_bundle

    return _legacy_result_from_ex_bundle(
        ExtractedInvoiceBundle(
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
    )


def _force_clarify(state, reasons, *, model):
    return {"verdict": nodes.REVIEW_VERDICT_CLARIFY, "question": "Please confirm this document."}


def setup_function(_):
    nodes.REVIEWER_FN = _force_clarify
    from tests.test_nodes import _legacy_result_from_ex_bundle

    nodes.EXTRACT_INVOICE_DOCUMENT_FN = lambda *a, **k: _legacy_result_from_ex_bundle(
        ExtractedInvoiceBundle(
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
    )


def teardown_function(_):
    nodes.REVIEWER_FN = nodes._reviewer_llm
    from invoice_processing.extract.process_invoice_document import process_invoice_document

    nodes.EXTRACT_INVOICE_DOCUMENT_FN = process_invoice_document


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


# =========================================================================== #
# Production driver roundtrip — run the REAL ``document_workflow_driver`` (the
# Step 6 dynamic spine) end-to-end through BOTH HITL interrupts and prove the
# side-effecting nodes (extract / consolidate) run EXACTLY ONCE across resumes.
# Mirrors the static-shape roundtrips above but exercises ``ctx.run_node``
# scheduling instead of a hand-built single-node workflow.
# =========================================================================== #


from accounting_agents.agent import document_workflow_driver  # noqa: E402
from invoice_processing.classify.document_classifier import (  # noqa: E402
    ClassificationResult,
)


def _unreconciled_bundle(*_a, **_k):
    """Extraction yields an UNRECONCILED invoice (total ≠ subtotal+gst)."""
    from tests.test_nodes import _legacy_result_from_ex_bundle

    return _legacy_result_from_ex_bundle(
        ExtractedInvoiceBundle(
            invoices=[
                ExtractedInvoice(
                    doc_type="invoice",
                    invoice_number="INV-DRV",
                    invoice_date="2025-01-15",
                    currency="SGD",
                    issuer_name="Acme Supplier",
                    bill_to_name="Client",
                    lines=[ExtractedLine(description="Goods", net_amount=100.0, gst_amount=9.0)],
                    subtotal=100.0,
                    gst_total=9.0,
                    total=999.0,  # deliberately wrong → reconcile fails
                )
            ]
        )
    )


def _driver_state(channel: str) -> dict:
    """Minimal state for a full driver pass (no pre-seeded normalized invoices)."""
    return {
        "op_id": f"{channel}:F1",
        "channel_id": channel,
        "file_id": "F1",
        "direction": "purchase",
        "tax_registered": True,
        "software": "qbs",
        "fye_month": 3,
        "client_id": "drv-client",
        nodes.ARTIFACT_NAME_KEY: nodes.ARTIFACT_NAME_FMT.format(file_id="F1"),
    }


def _new_driver_runner(session_id: str, counters: dict):
    """Runner whose root agent is the REAL ``document_workflow_driver``.

    ``counters`` is mutated to count classify / extract / consolidate calls so a
    test can prove the side-effecting nodes run exactly once across resumes.
    """
    wf = Workflow(name="document_workflow", edges=[(START, document_workflow_driver)])
    app = App(
        name="acc",
        root_agent=wf,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    svc = FirestoreSessionService(client=FakeFirestore())
    asyncio.run(
        svc.create_session(
            app_name="acc", user_id=session_id, session_id=session_id,
            state=_driver_state(session_id),
        )
    )
    artifacts = InMemoryArtifactService()
    asyncio.run(
        artifacts.save_artifact(
            app_name="acc", user_id=session_id, session_id=session_id,
            filename=nodes.ARTIFACT_NAME_FMT.format(file_id="F1"),
            artifact=types.Part(
                inline_data=types.Blob(data=b"%PDF-1.4 stub", mime_type="application/pdf")
            ),
        )
    )
    return Runner(app=app, session_service=svc, artifact_service=artifacts), svc


def test_driver_full_pass_both_interrupts_side_effects_once():
    """REAL driver: mid-flow :review pause → resume → terminal pause → approve.

    Proves under dynamic ``ctx.run_node`` scheduling:
    - the mid-flow ``:review`` interrupt id surfaces,
    - the terminal approval interrupt id surfaces after the review is resolved,
    - the terminal ``ApproveDecision`` is applied (APPROVAL_STATUS_KEY → approve),
    - the side-effecting nodes (extract + consolidate) run EXACTLY ONCE despite
      the driver replaying from the top after each pause (rerun_on_resume skips
      already-checkpointed sub-nodes).
    """
    counters = {"classify": 0, "extract": 0, "consolidate": 0}

    # Fakes: classify → invoice; extract → unreconciled (trips detect_struggle);
    # reviewer → clarify (forces the mid-flow human pause). All seams are restored
    # in teardown_function below (which already resets REVIEWER_FN / EXTRACT_*).
    real_classify = nodes.CLASSIFY_FN
    real_direction = nodes.DIRECTION_FN
    real_consolidate = nodes.consolidate_node

    def _count_classify(data, mime, *, model):
        counters["classify"] += 1
        return ClassificationResult(doc_type="invoice", confidence=0.95, reason="test")

    def _count_extract(*a, **k):
        counters["extract"] += 1
        return _unreconciled_bundle()

    async def _count_consolidate(ctx):
        counters["consolidate"] += 1
        return await real_consolidate._func(ctx)

    nodes.CLASSIFY_FN = _count_classify
    nodes.DIRECTION_FN = lambda cls, **k: "purchase"
    nodes.EXTRACT_INVOICE_DOCUMENT_FN = _count_extract
    nodes.REVIEWER_FN = _force_clarify
    nodes.consolidate_node = _count_consolidate
    try:
        runner, svc = _new_driver_runner("D1", counters)

        # RUN 1: pauses mid-flow at the ":review" interrupt.
        ids1 = _collect_interrupt_ids(
            runner, "D1", types.Content(parts=[types.Part(text="process")])
        )
        assert ids1 == ["D1:F1:review"]

        # RUN 2: resume the review with confirm_as_is → the still-unreconciled
        # invoice trips the SEPARATE terminal gate, which pauses.
        review_resume = build_resume_message(
            "D1:F1:review", ReviewClarifyDecision(action="confirm_as_is")
        )
        ids2 = _collect_interrupt_ids(runner, "D1", review_resume)
        assert ids2 == ["D1:F1"]
        assert "D1:F1:review" not in ids2

        # RUN 3: resume the terminal gate with approve → it resolves, decision
        # threaded into apply_decision_node, spine runs to deliver.
        approve_resume = build_resume_message("D1:F1", ApproveDecision(decision="approve"))
        ids3 = _collect_interrupt_ids(runner, "D1", approve_resume)
        assert ids3 == []

        got = asyncio.run(svc.get_session(app_name="acc", user_id="D1", session_id="D1"))
        assert got.state.get(nodes.APPROVAL_STATUS_KEY) == "approve"
        assert got.state.get("review_clarify_action") == "confirm_as_is"
        assert got.state.get("delivered") is True

        # The make-or-break invariant: side-effecting nodes ran EXACTLY ONCE
        # across the two resumes (ctx.run_node fast-forwards checkpointed nodes).
        assert counters["extract"] == 1
        assert counters["consolidate"] == 1
        assert counters["classify"] == 1
    finally:
        nodes.CLASSIFY_FN = real_classify
        nodes.DIRECTION_FN = real_direction
        nodes.consolidate_node = real_consolidate


def test_driver_full_pass_auto_approve_no_pause():
    """REAL driver: a CLEAN invoice flows straight through with NO pause.

    Proves the happy path: clean extraction → review waves through (ZERO LLM) →
    auto-approve gate → deliver, all in one run, side effects once.
    """
    counters = {"extract": 0, "consolidate": 0}
    real_classify = nodes.CLASSIFY_FN
    real_direction = nodes.DIRECTION_FN
    real_consolidate = nodes.consolidate_node

    def _count_extract(*a, **k):
        counters["extract"] += 1
        return _fake_clean_bundle()

    async def _count_consolidate(ctx):
        counters["consolidate"] += 1
        return await real_consolidate._func(ctx)

    nodes.CLASSIFY_FN = lambda d, m, *, model: ClassificationResult(
        doc_type="invoice", confidence=0.95, reason="test"
    )
    nodes.DIRECTION_FN = lambda cls, **k: "purchase"
    nodes.EXTRACT_INVOICE_DOCUMENT_FN = _count_extract
    nodes.consolidate_node = _count_consolidate
    try:
        runner, svc = _new_driver_runner("D2", counters)
        ids = _collect_interrupt_ids(
            runner, "D2", types.Content(parts=[types.Part(text="process")])
        )
        assert ids == []  # no pause at all
        got = asyncio.run(svc.get_session(app_name="acc", user_id="D2", session_id="D2"))
        assert got.state.get(nodes.APPROVAL_STATUS_KEY) == "auto_approved"
        assert got.state.get("delivered") is True
        assert counters["extract"] == 1
        assert counters["consolidate"] == 1
    finally:
        nodes.CLASSIFY_FN = real_classify
        nodes.DIRECTION_FN = real_direction
        nodes.consolidate_node = real_consolidate
