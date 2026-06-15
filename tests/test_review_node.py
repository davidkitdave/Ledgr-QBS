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
        lines=[InvoiceLine(description="Goods", net_amount=100.0, gst_amount=9.0)],
    )
    defaults.update(overrides)
    return NormalizedInvoice(**defaults)


def _state(invoices, *, doc_type="invoice", confidence=0.95, **extra) -> dict:
    state = {
        nodes.ARTIFACT_NAME_KEY: nodes.ARTIFACT_NAME_FMT.format(file_id="F1"),
        nodes.NORMALIZED_KEY: [nodes._inv_to_dict(i) for i in invoices],
        nodes.DOC_TYPE_KEY: doc_type,
        nodes.CLASSIFY_CONFIDENCE_KEY: confidence,
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


@pytest.fixture(autouse=True)
def _restore_seams():
    saved = {n: getattr(nodes, n) for n in ("REVIEWER_FN", "EXTRACT_BUNDLE_FN")}
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
    calls = {"n": 0}

    def fake_reviewer(state, reasons, *, model):
        calls["n"] += 1
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    nodes.REVIEWER_FN = fake_reviewer
    inv = _clean_invoice(reconciled=False, reconcile_note="totals off")
    ctx = FakeContext(_state([inv]))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    assert calls["n"] == 1
    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_OK
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

    def fake_extract(data, mime, *, model, hint=None):
        extract_calls["n"] += 1
        extract_calls["hints"].append(hint)
        # The re-extraction now yields a CLEAN invoice.
        return ExtractedInvoiceBundle(
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
                )
            ]
        )

    nodes.REVIEWER_FN = fake_reviewer
    nodes.EXTRACT_BUNDLE_FN = fake_extract

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

    def fake_extract(data, mime, *, model, hint=None):
        extract_calls["n"] += 1
        # Re-extraction stays bad (still empty lines) so signals keep tripping.
        return ExtractedInvoiceBundle(
            invoices=[
                ExtractedInvoice(
                    doc_type="invoice",
                    invoice_number="INV-1",
                    invoice_date="2025-01-15",
                    currency="SGD",
                    lines=[],
                    total=109.0,
                )
            ]
        )

    nodes.REVIEWER_FN = always_hints
    nodes.EXTRACT_BUNDLE_FN = fake_extract

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
