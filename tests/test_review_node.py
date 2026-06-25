"""Behavioral tests for ``review_extraction_node`` + ``_run_reviewer_loop``.

Hermetic: ``REVIEWER_FN`` and ``EXTRACT_BUNDLE_FN`` are swapped for fakes (NO
Gemini / network). Covers:
- ok / no-signal happy path → REVIEWER_FN NOT called, falls through to categorize
- hints_needed → re-extract called EXACTLY once, then re-review
- §9.3 ceiling enforced (≤2 reviews, ≤1 re-extract) via call counts
- §0.5-B: a silent/empty reviewer degrades to user_clarify
- all review_* state keys written
- an unreconciled tripped doc actually invokes REVIEWER_FN
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from accounting_agents import nodes
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice
from invoice_processing.extract.invoice_extractor import (
    ExtractedInvoice,
    ExtractedInvoiceBundle,
    ExtractedLine,
)


class FakeContext:
    def __init__(self, state: dict):
        self.state = dict(state)

    async def load_artifact(self, filename, version=None):
        inline = SimpleNamespace(data=b"%PDF stub", mime_type="application/pdf")
        return SimpleNamespace(inline_data=inline)


def _clean_invoice(**overrides) -> NormalizedInvoice:
    defaults = dict(
        invoice_number="INV-1",
        invoice_date=date(2025, 1, 15),
        doc_total=109.0,
        reconciled=True,
        our_gst_registered=True,
        lines=[InvoiceLine(description="Goods", net_amount=100.0, gst_amount=9.0,
                            account_code="6100")],
    )
    defaults.update(overrides)
    return NormalizedInvoice(**defaults)


def _state(invoices, *, doc_type="invoice", confidence=0.95, **extra) -> dict:
    state = {
        nodes.ARTIFACT_NAME_KEY: nodes.ARTIFACT_NAME_FMT.format(file_id="F1"),
        nodes.NORMALIZED_KEY: [nodes._inv_to_dict(i) for i in invoices],
        nodes.DOC_TYPE_KEY: doc_type,
        nodes.CLASSIFY_CONFIDENCE_KEY: confidence,
        # WS-1.5: pre-set tax_jurisdiction so the jurisdiction_unresolved
        # flag does NOT fire by default. Tests that want to assert that
        # flag specifically override this with tax_jurisdiction=None.
        nodes.TAX_JURISDICTION_KEY: "SINGAPORE",
        "op_id": "C1:F1",
        "tax_registered": True,
        "direction": "purchase",
    }
    state.update(extra)
    return state


async def _drive(node_coro_gen):
    """Drive an async generator node to completion, returning any yielded value."""
    yielded = []
    try:
        async for item in node_coro_gen:
            yielded.append(item)
    except StopAsyncIteration:
        pass
    return yielded


def _still_unreconciled_extract_result():
    """Re-extract stub that keeps the doc unreconciled (default for legacy tests)."""
    from tests.test_nodes import _legacy_result_from_ex_bundle

    return _legacy_result_from_ex_bundle(
        ExtractedInvoiceBundle(
            invoices=[
                ExtractedInvoice(
                    doc_type="invoice",
                    invoice_number="INV-1",
                    invoice_date="2025-01-15",
                    currency="SGD",
                    issuer_name="Acme Supplier",
                    bill_to_name="Client",
                    lines=[ExtractedLine(description="Goods", net_amount=100.0, gst_amount=9.0)],
                    subtotal=100.0,
                    gst_total=9.0,
                    total=120.0,
                    issuer_tax_system="NONE",
                )
            ]
        )
    )


def _reconciled_extract_result():
    """Re-extract stub that produces a fully reconciled invoice (WS4 success path)."""
    from tests.test_nodes import _legacy_result_from_ex_bundle

    result = _legacy_result_from_ex_bundle(
        ExtractedInvoiceBundle(
            invoices=[
                ExtractedInvoice(
                    doc_type="invoice",
                    invoice_number="INV-1",
                    invoice_date="2025-01-15",
                    currency="SGD",
                    issuer_name="Acme Supplier",
                    bill_to_name="Client",
                    lines=[ExtractedLine(description="Goods", net_amount=100.0, gst_amount=9.0)],
                    subtotal=100.0,
                    gst_total=9.0,
                    total=109.0,
                    issuer_tax_system="NONE",
                )
            ]
        )
    )
    # WS-1.5: simulate categorize_node filling in account_code on the line
    # so the blank_account_code flag does not fire on the post-extract state.
    for inv in result.normalized:
        for ln in inv.lines:
            ln.account_code = "6100"
    return result


@pytest.fixture(autouse=True)
def _restore_seams():
    saved = {n: getattr(nodes, n) for n in ("REVIEWER_FN", "EXTRACT_INVOICE_DOCUMENT_FN")}
    nodes.EXTRACT_INVOICE_DOCUMENT_FN = lambda data, mime, **kw: _still_unreconciled_extract_result()
    yield
    for n, fn in saved.items():
        setattr(nodes, n, fn)


# --------------------------------------------------------------------------- #
# Happy path: no signal → REVIEWER_FN NOT called, verdict ok, falls through.
# --------------------------------------------------------------------------- #


def test_no_signal_skips_reviewer_and_marks_ok():
    calls = {"n": 0}

    def fake_reviewer(state, reasons, *, model):
        calls["n"] += 1
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    nodes.REVIEWER_FN = fake_reviewer
    ctx = FakeContext(_state([_clean_invoice()]))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    # ZERO-LLM happy path: the critic is never invoked.
    assert calls["n"] == 0
    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_OK
    assert ctx.state[nodes.REVIEW_REASON_KEY] == []
    # Normalized payload is untouched (falls through to categorize unchanged).
    assert ctx.state[nodes.NORMALIZED_KEY][0]["invoice_number"] == "INV-1"


# --------------------------------------------------------------------------- #
# A tripped (unreconciled) doc DOES invoke the reviewer.
# --------------------------------------------------------------------------- #


def test_unreconciled_invokes_reviewer():
    """Reviewer IS invoked on a hard signal (unreconciled). After the fix (ADR-0017 §3
    safety gap), the critic's OK cannot clear a remaining hard signal — detect_struggle
    is re-checked deterministically after the loop and the doc escalates to CLARIFY."""
    calls = {"n": 0}

    def fake_reviewer(state, reasons, *, model):
        calls["n"] += 1
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    nodes.REVIEWER_FN = fake_reviewer
    inv = _clean_invoice(reconciled=False, reconcile_note="totals off")
    ctx = FakeContext(_state([inv]))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    assert calls["n"] == 1
    # Hard signal still present after critic returned OK → must escalate, not proceed.
    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_CLARIFY
    assert any(r.startswith("unreconciled") for r in ctx.state[nodes.REVIEW_REASON_KEY])


# --------------------------------------------------------------------------- #
# hints_needed → re-extract EXACTLY once, then re-review.
# --------------------------------------------------------------------------- #


def test_hints_needed_reextracts_exactly_once_then_rereviews():
    reviewer_calls = {"n": 0}
    extract_calls = {"n": 0, "hints": []}

    def fake_reviewer(state, reasons, *, model):
        reviewer_calls["n"] += 1
        if reviewer_calls["n"] == 1:
            return {"verdict": nodes.REVIEW_VERDICT_HINTS, "hint": "read the summary page"}
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    def fake_extract(data, mime, **kw):
        extract_calls["n"] += 1
        extract_calls["hints"].append(kw.get("hint"))
        from tests.test_nodes import _legacy_result_from_ex_bundle
        result = _legacy_result_from_ex_bundle(
            ExtractedInvoiceBundle(
            invoices=[
                ExtractedInvoice(
                    doc_type="invoice",
                    invoice_number="INV-1",
                    invoice_date="2025-01-15",
                    currency="SGD",
                    issuer_name="Acme Supplier",
                    bill_to_name="Client",
                    lines=[ExtractedLine(description="Goods", net_amount=100.0, gst_amount=9.0)],
                    subtotal=100.0,
                    gst_total=9.0,
                    total=109.0,
                    issuer_tax_system="NONE",
                )
            ]
        ))
        # WS-1.5: simulate categorize_node filling in account_code on the
        # line so the blank_account_code flag does not fire.
        for inv in result.normalized:
            for ln in inv.lines:
                ln.account_code = "6100"
        return result

    nodes.REVIEWER_FN = fake_reviewer
    nodes.EXTRACT_INVOICE_DOCUMENT_FN = fake_extract

    # Start tripped via an empty line invoice.
    inv = _clean_invoice(lines=[])
    ctx = FakeContext(_state([inv]))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    assert extract_calls["n"] == 1  # re-extract exactly once
    assert extract_calls["hints"] == ["read the summary page"]
    assert ctx.state["review_reextract_count"] == 1
    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_OK
    # After the clean re-extraction the doc has its line back.
    assert ctx.state[nodes.NORMALIZED_KEY][0]["lines"]


# --------------------------------------------------------------------------- #
# §9.3 ceiling: at most 2 reviews + 1 re-extract, then circuit-break.
# --------------------------------------------------------------------------- #


def test_ceiling_enforced_two_reviews_one_reextract():
    reviewer_calls = {"n": 0}
    extract_calls = {"n": 0}

    def always_hints(state, reasons, *, model):
        reviewer_calls["n"] += 1
        return {"verdict": nodes.REVIEW_VERDICT_HINTS, "hint": "try again"}

    def fake_extract(data, mime, **kw):
        extract_calls["n"] += 1
        from tests.test_nodes import _legacy_result_from_ex_bundle
        return _legacy_result_from_ex_bundle(
            ExtractedInvoiceBundle(
            invoices=[
                ExtractedInvoice(
                    doc_type="invoice",
                    invoice_number="INV-1",
                    invoice_date="2025-01-15",
                    currency="SGD",
                    lines=[],
                    total=109.0,
                    gst_total=0.0,
                    issuer_tax_system="NONE",
                    subtotal=0.0,
                )
            ]
        ))

    nodes.REVIEWER_FN = always_hints
    nodes.EXTRACT_INVOICE_DOCUMENT_FN = fake_extract

    inv = _clean_invoice(lines=[])
    ctx = FakeContext(_state([inv]))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    # Ceiling: ≤2 reviewer calls, ≤1 re-extract; circuit-break to human.
    assert reviewer_calls["n"] <= nodes.REVIEW_MAX_REVIEWS
    assert extract_calls["n"] <= nodes.REVIEW_MAX_REEXTRACTS
    assert ctx.state["review_attempts"] <= nodes.REVIEW_MAX_REVIEWS
    assert ctx.state["review_reextract_count"] <= nodes.REVIEW_MAX_REEXTRACTS
    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_CLARIFY


# --------------------------------------------------------------------------- #
# §0.5-B: a silent / empty reviewer degrades to user_clarify (no crash).
# --------------------------------------------------------------------------- #


def test_empty_reviewer_degrades_to_user_clarify():
    def silent_reviewer(state, reasons, *, model):
        return {}  # no verdict key — must degrade, not crash

    nodes.REVIEWER_FN = silent_reviewer
    inv = _clean_invoice(reconciled=False)
    ctx = FakeContext(_state([inv]))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_CLARIFY
    assert ctx.state.get("review_question")  # a human prompt was prepared


def test_all_review_state_keys_written_on_trip():
    nodes.REVIEWER_FN = lambda state, reasons, *, model: {"verdict": nodes.REVIEW_VERDICT_OK}
    inv = _clean_invoice(reconciled=False)
    ctx = FakeContext(_state([inv]))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    assert nodes.REVIEW_VERDICT_KEY in ctx.state
    assert nodes.REVIEW_REASON_KEY in ctx.state
    assert "review_attempts" in ctx.state


# --------------------------------------------------------------------------- #
# WS4 — self-healing reconcile re-read (unreconciled-only)
# --------------------------------------------------------------------------- #


def test_unreconciled_only_auto_retry_proceeds_when_reconciled():
    """Unreconciled-only → one totals re-extract; if reconciled, no HITL."""
    reviewer_calls = {"n": 0}
    extract_calls = {"n": 0, "hints": []}

    def fake_reviewer(state, reasons, *, model):
        reviewer_calls["n"] += 1
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    def fake_extract(data, mime, **kw):
        extract_calls["n"] += 1
        extract_calls["hints"].append(kw.get("hint"))
        return _reconciled_extract_result()

    nodes.REVIEWER_FN = fake_reviewer
    nodes.EXTRACT_INVOICE_DOCUMENT_FN = fake_extract
    inv = _clean_invoice(reconciled=False, reconcile_note="totals off by $10")
    ctx = FakeContext(_state([inv]))

    yielded = asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    assert extract_calls["n"] == 1
    assert extract_calls["hints"] == [nodes.RECONCILE_REREAD_HINT]
    assert ctx.state[nodes.RECONCILE_REEXTRACT_ATTEMPTED_KEY] is True
    assert reviewer_calls["n"] == 0, "No critic call when reconcile re-read fixes totals"
    assert yielded == []
    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_OK


def test_unreconciled_only_still_unreconciled_after_retry_escalates():
    """Unreconciled-only → one retry still bad → escalate as today."""
    reviewer_calls = {"n": 0}
    extract_calls = {"n": 0}

    def fake_reviewer(state, reasons, *, model):
        reviewer_calls["n"] += 1
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    def fake_extract(data, mime, **kw):
        extract_calls["n"] += 1
        return _still_unreconciled_extract_result()

    nodes.REVIEWER_FN = fake_reviewer
    nodes.EXTRACT_INVOICE_DOCUMENT_FN = fake_extract
    inv = _clean_invoice(reconciled=False, reconcile_note="totals off by $10")
    ctx = FakeContext(_state([inv]))

    yielded = asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    assert extract_calls["n"] == 1
    assert ctx.state[nodes.RECONCILE_REEXTRACT_ATTEMPTED_KEY] is True
    assert reviewer_calls["n"] >= 1
    assert len(yielded) == 1
    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_CLARIFY


def test_mixed_unreconciled_and_soft_no_auto_reconcile_retry():
    """Mixed signals (unreconciled + low confidence) → no WS4 auto-retry."""
    extract_calls = {"n": 0}

    def fake_extract(data, mime, **kw):
        extract_calls["n"] += 1
        return _reconciled_extract_result()

    nodes.REVIEWER_FN = lambda state, reasons, *, model: {"verdict": nodes.REVIEW_VERDICT_OK}
    nodes.EXTRACT_INVOICE_DOCUMENT_FN = fake_extract
    inv = _clean_invoice(reconciled=False, reconcile_note="totals off by $10")
    ctx = FakeContext(_state([inv], confidence=0.50))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    assert extract_calls["n"] == 0
    assert nodes.RECONCILE_REEXTRACT_ATTEMPTED_KEY not in ctx.state


def test_approval_gate_unreconciled_only_auto_retry_proceeds_when_reconciled():
    """approval_gate: reconcile-only reasons → one re-extract then auto-approve."""
    extract_calls = {"n": 0, "hints": []}

    def fake_extract(data, mime, **kw):
        extract_calls["n"] += 1
        extract_calls["hints"].append(kw.get("hint"))
        return _reconciled_extract_result()

    nodes.EXTRACT_INVOICE_DOCUMENT_FN = fake_extract
    inv = _clean_invoice(reconciled=False, reconcile_note="totals off by $10")
    ctx = FakeContext(_state([inv]))

    async def _run_gate():
        events = []
        async for event in nodes.approval_gate(ctx):
            events.append(event)
        return events

    events = asyncio.run(_run_gate())

    assert extract_calls["n"] == 1
    assert extract_calls["hints"] == [nodes.RECONCILE_REREAD_HINT]
    assert ctx.state[nodes.RECONCILE_REEXTRACT_ATTEMPTED_KEY] is True
    assert events == []
    assert ctx.state[nodes.APPROVAL_STATUS_KEY] == "auto_approved"


def test_approval_gate_mixed_unreconciled_and_tax_flagged_no_auto_retry():
    """Mixed unreconciled + tax_flagged → escalate immediately, no WS4 retry."""
    from google.adk.events import RequestInput

    extract_calls = {"n": 0}

    def fake_extract(data, mime, **kw):
        extract_calls["n"] += 1
        return _reconciled_extract_result()

    nodes.EXTRACT_INVOICE_DOCUMENT_FN = fake_extract
    inv = _clean_invoice(
        reconciled=False,
        reconcile_note="totals off by $10",
        lines=[
            InvoiceLine(
                description="Goods",
                net_amount=100.0,
                gst_amount=9.0,
                tax_flagged=True,
                tax_reason="rate mismatch",
            )
        ],
    )
    ctx = FakeContext(_state([inv]))

    async def _run_gate():
        events = []
        async for event in nodes.approval_gate(ctx):
            events.append(event)
        return events

    events = asyncio.run(_run_gate())

    assert extract_calls["n"] == 0
    assert nodes.RECONCILE_REEXTRACT_ATTEMPTED_KEY not in ctx.state
    assert len(events) == 1
    assert isinstance(events[0], RequestInput)


# ---------------------------------------------------------------------------
# FIX 1 — direction-uncertain reasons must NOT be treated as reconcile-only
# ---------------------------------------------------------------------------

def test_is_reconcile_only_excludes_direction_reasons():
    """A reason mentioning 'direction' is NOT reconcile-only (WS4 fix)."""
    direction_reason = "net_amount: not reconciled (direction unknown — debit or credit?)"
    totals_reason = "net_amount: not reconciled (totals off by $10)"

    # Pure direction reason → NOT reconcile-only
    assert not nodes._is_reconcile_only_needs_review_reasons([direction_reason])

    # Pure totals reason → IS reconcile-only
    assert nodes._is_reconcile_only_needs_review_reasons([totals_reason])

    # Mixed: direction + totals → NOT reconcile-only (any direction reason disqualifies)
    assert not nodes._is_reconcile_only_needs_review_reasons([totals_reason, direction_reason])

    # Empty → always False
    assert not nodes._is_reconcile_only_needs_review_reasons([])


def test_approval_gate_direction_uncertain_does_not_auto_retry():
    """approval_gate: direction-uncertain reason escalates — no reconcile re-extract."""

    extract_calls = {"n": 0}

    def fake_extract(data, mime, **kw):
        extract_calls["n"] += 1
        return _reconciled_extract_result()

    nodes.EXTRACT_INVOICE_DOCUMENT_FN = fake_extract
    # reconcile_note contains "direction" — should NOT trigger WS4 auto-retry
    inv = _clean_invoice(
        reconciled=False,
        reconcile_note="net_amount: not reconciled (direction unknown — debit or credit?)",
    )
    ctx = FakeContext(_state([inv]))

    async def _run_gate():
        events = []
        async for event in nodes.approval_gate(ctx):
            events.append(event)
        return events

    asyncio.run(_run_gate())

    assert extract_calls["n"] == 0, "direction-uncertain doc must NOT trigger reconcile re-extract"
    assert nodes.RECONCILE_REEXTRACT_ATTEMPTED_KEY not in ctx.state


# --------------------------------------------------------------------------- #
# Regression: review_question persisted to state in BOTH escalation branches
# --------------------------------------------------------------------------- #


def test_review_question_persisted_soft_only_branch():
    """Soft-only path: critic returns CLARIFY → state['review_question'] is non-empty."""

    def clarify_reviewer(state, reasons, *, model):
        return {"verdict": nodes.REVIEW_VERDICT_CLARIFY, "question": None}

    nodes.REVIEWER_FN = clarify_reviewer
    # Low classify confidence is a soft signal.
    inv = _clean_invoice(lines=[])
    ctx = FakeContext(_state([inv], confidence=0.45))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    q = ctx.state.get("review_question")
    assert q, "review_question must be persisted to state when soft-only branch escalates"
    assert len(q.strip()) > 0


def test_review_question_persisted_hard_signal_ok_verdict_branch():
    """Hard-signal path: critic returns OK but hard signal survives recheck →
    state['review_question'] is non-empty (the bug: it was missing before this fix)."""

    def ok_reviewer(state, reasons, *, model):
        # Critic says OK, but the hard signal (unreconciled) cannot be cleared.
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    nodes.REVIEWER_FN = ok_reviewer
    inv = _clean_invoice(reconciled=False, reconcile_note="totals do not match")
    ctx = FakeContext(_state([inv]))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    # Hard signal still present → must escalate with CLARIFY verdict.
    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_CLARIFY
    q = ctx.state.get("review_question")
    assert q, (
        "review_question must be persisted to state when hard signal survives critic OK "
        "(fixes the empty card body / SlackApiError regression)"
    )
    assert len(q.strip()) > 0
