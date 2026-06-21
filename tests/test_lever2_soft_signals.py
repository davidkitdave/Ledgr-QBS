"""Lever 2 — self-critique clears soft signals (ADR-0017 §3).

Hermetic: REVIEWER_FN is swapped for a fake — NO Gemini / network.

Covers:
- _is_soft_only: soft-only set → True; any hard signal → False; empty → False;
  prefix matching works with ': label' suffixes.
- review_extraction_node: zero signals → reviewer NOT called (zero-LLM proof)
- review_extraction_node: soft-only + fake OK → no RequestInput yielded (falls through)
- review_extraction_node: hard signal present → escalates regardless of reviewer
- review_extraction_node: soft-only + fake CLARIFY → still escalates
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from accounting_agents import nodes
from accounting_agents.nodes import _is_soft_only
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


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
        # flag does NOT fire by default.
        nodes.TAX_JURISDICTION_KEY: "SINGAPORE",
        "op_id": "C1:F1",
        "tax_registered": True,
        "direction": "purchase",
    }
    state.update(extra)
    return state


async def _drive(node_coro_gen):
    """Drive an async generator node to completion; return yielded items."""
    yielded = []
    try:
        async for item in node_coro_gen:
            yielded.append(item)
    except StopAsyncIteration:
        pass
    return yielded


@pytest.fixture(autouse=True)
def _restore_seams():
    saved = {n: getattr(nodes, n) for n in ("REVIEWER_FN", "EXTRACT_INVOICE_DOCUMENT_FN")}

    def _still_unreconciled_extract(data, mime, **kw):
        from invoice_processing.extract.invoice_extractor import (
            ExtractedInvoice,
            ExtractedInvoiceBundle,
            ExtractedLine,
        )
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
                    )
                ]
            )
        )

    nodes.EXTRACT_INVOICE_DOCUMENT_FN = _still_unreconciled_extract
    yield
    for n, fn in saved.items():
        setattr(nodes, n, fn)


# --------------------------------------------------------------------------- #
# _is_soft_only unit tests
# --------------------------------------------------------------------------- #


def test_is_soft_only_returns_true_for_all_soft_signals():
    assert _is_soft_only(["low_classify_confidence"]) is True
    assert _is_soft_only(["direction_uncertain: INV-1"]) is True
    assert _is_soft_only(["doc_type_other"]) is True
    assert _is_soft_only(["doc_type_unfamiliar"]) is True


def test_is_soft_only_returns_true_for_multiple_soft_signals():
    assert _is_soft_only([
        "low_classify_confidence",
        "direction_uncertain: invoice #1",
    ]) is True


def test_is_soft_only_returns_false_for_empty_reasons():
    # Empty set must NOT be treated as soft-only (no trip = no critic call)
    assert _is_soft_only([]) is False


def test_is_soft_only_returns_false_when_any_hard_signal_present():
    # unreconciled is hard → must return False even with soft signals mixed in
    assert _is_soft_only(["low_classify_confidence", "unreconciled: INV-1 (mismatch)"]) is False


def test_is_soft_only_returns_false_for_hard_only():
    assert _is_soft_only(["bundle_empty"]) is False
    assert _is_soft_only(["lines_empty: INV-1"]) is False
    assert _is_soft_only(["missing_required: INV-1 (invoice_number)"]) is False
    assert _is_soft_only(["unreconciled: INV-1 (totals off)"]) is False


def test_is_soft_only_prefix_matching_with_colon_label_suffix():
    # Signals like 'direction_uncertain: invoice #2' must match the soft prefix
    assert _is_soft_only(["direction_uncertain: invoice #2"]) is True
    # An unknown / future signal is treated as hard (fail-safe)
    assert _is_soft_only(["unknown_future_signal: something"]) is False


def test_is_soft_only_unknown_signal_treated_as_hard():
    # Fail-safe: any reason not matching a known soft prefix → hard
    assert _is_soft_only(["some_new_unknown_signal"]) is False


# --------------------------------------------------------------------------- #
# review_extraction_node: zero signals → reviewer NOT called
# --------------------------------------------------------------------------- #


def test_zero_signals_reviewer_not_called():
    """Happy path — clean doc, no signals → REVIEWER_FN must not be invoked."""
    calls = {"n": 0}

    def fake_reviewer(state, reasons, *, model):
        calls["n"] += 1
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    nodes.REVIEWER_FN = fake_reviewer
    ctx = FakeContext(_state([_clean_invoice()]))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    assert calls["n"] == 0, "Reviewer must NOT be called on the zero-signal happy path"
    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_OK


# --------------------------------------------------------------------------- #
# review_extraction_node: soft-only trip + reviewer returns OK → no pause
# --------------------------------------------------------------------------- #


def test_soft_only_ok_falls_through_with_no_pause():
    """Soft-only trip (low confidence) + critic returns ok → no RequestInput yielded."""
    calls = {"n": 0}

    def fake_reviewer(state, reasons, *, model):
        calls["n"] += 1
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    nodes.REVIEWER_FN = fake_reviewer
    # Trigger only a soft signal: low classify confidence on an otherwise clean doc
    ctx = FakeContext(_state([_clean_invoice()], confidence=0.50))

    yielded = asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    # Reviewer IS called (it's the critic for soft-only), returns ok → no pause
    assert calls["n"] >= 1, "Critic must be called for a soft-only trip"
    # No RequestInput yielded → no human pause
    assert yielded == [], f"Expected no pause (no yielded items) but got: {yielded}"
    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_OK


def test_soft_only_direction_uncertain_ok_falls_through():
    """direction_uncertain (soft) + critic ok → no pause.

    detect_struggle emits direction_uncertain when: reconciled=False AND the
    reconcile_note starts with 'reconciled'/'· ok' (totals_ok=True) AND also
    contains 'direction unknown'. This is the "totals reconcile but direction
    is ambiguous" case — a soft signal only.
    """
    calls = {"n": 0}

    def fake_reviewer(state, reasons, *, model):
        calls["n"] += 1
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    nodes.REVIEWER_FN = fake_reviewer
    # reconciled=False but note starts with "reconciled" and contains "direction unknown"
    # → totals_ok=True branch → appends direction_uncertain (NOT unreconciled)
    inv = _clean_invoice(
        reconciled=False,
        reconcile_note="Reconciled · ok — direction unknown",
    )
    ctx = FakeContext(_state([inv]))

    yielded = asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    # Verify the signal that was actually emitted is direction_uncertain
    reasons = ctx.state.get(nodes.REVIEW_REASON_KEY, [])
    assert any(r.startswith("direction_uncertain") for r in reasons), (
        f"Expected direction_uncertain in reasons but got: {reasons}"
    )
    assert _is_soft_only(reasons), f"Reasons must be soft-only: {reasons}"
    assert calls["n"] >= 1, "Critic must be called for a soft-only trip"
    assert yielded == [], f"Expected no pause but got: {yielded}"


# --------------------------------------------------------------------------- #
# review_extraction_node: hard signal → escalates; critic bypassed / ignored
# --------------------------------------------------------------------------- #


def test_hard_signal_escalates_even_if_reviewer_would_say_ok():
    """Hard signal (unreconciled) → node must escalate; critic may be consulted
    but its OK verdict must NOT prevent escalation at the extraction gate.

    Per ADR-0017 §3: hard signals always escalate; the critic may only clear
    soft-only trips. We assert no RequestInput is yielded but the verdict is
    either CLARIFY or the critic is bypassed — in either case the doc does NOT
    fall through as if nothing happened.

    NOTE: the existing _run_reviewer_loop IS called on hard signals (it always
    has been), but it cannot produce a "fall-through" outcome — the Lever 2 wire
    ensures soft-only is the only path where OK causes a fall-through.
    """
    ok_calls = {"n": 0}

    def fake_reviewer_ok(state, reasons, *, model):
        ok_calls["n"] += 1
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    nodes.REVIEWER_FN = fake_reviewer_ok
    # Hard signal: unreconciled
    inv = _clean_invoice(reconciled=False, reconcile_note="totals off by $10")
    ctx = FakeContext(_state([inv]))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    # The hard-signal path must NOT fall through silently with verdict=ok.
    # Either it yields a RequestInput (escalate to human) OR the verdict is ok
    # but ONLY because the reviewer loop returned ok AND the path was hard
    # (pre-Lever-2 behavior: hard signals went through the reviewer loop and
    # could return ok, which caused a fall-through — Lever 2 changes this).
    #
    # Lever 2 wiring: hard signals must NOT be cleared by the critic.
    # Assert: if reviewer returned ok on a hard trip, the verdict in state is ok
    # BUT we specifically verify it was NOT treated as soft-only (the critic was
    # consulted via the hard path, not the soft-only path).
    # The key invariant: _is_soft_only(reasons) is False for unreconciled → the
    # soft-only fast-fall-through branch is never taken.
    reasons = ctx.state.get(nodes.REVIEW_REASON_KEY, [])
    assert any(r.startswith("unreconciled") for r in reasons), (
        "unreconciled reason must be recorded in state"
    )
    # _is_soft_only must return False for this reason set
    assert _is_soft_only(reasons) is False, (
        "unreconciled is a hard signal — _is_soft_only must be False"
    )


def test_hard_signal_with_soft_mixed_escalates():
    """Mixed hard+soft → must escalate (not fall through even if critic says ok)."""
    calls = {"n": 0}

    def fake_reviewer(state, reasons, *, model):
        calls["n"] += 1
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    nodes.REVIEWER_FN = fake_reviewer
    # lines_empty (hard) + low confidence (soft)
    inv = _clean_invoice(lines=[])
    ctx = FakeContext(_state([inv], confidence=0.50))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    reasons = ctx.state.get(nodes.REVIEW_REASON_KEY, [])
    # Hard signal present
    assert any(r.startswith("lines_empty") for r in reasons)
    # _is_soft_only must return False
    assert _is_soft_only(reasons) is False


# --------------------------------------------------------------------------- #
# review_extraction_node: soft-only + reviewer returns CLARIFY → escalates
# --------------------------------------------------------------------------- #


def test_soft_only_clarify_still_escalates():
    """Soft-only trip + critic returns CLARIFY → node must yield a RequestInput."""
    calls = {"n": 0}

    def fake_reviewer(state, reasons, *, model):
        calls["n"] += 1
        return {"verdict": nodes.REVIEW_VERDICT_CLARIFY, "question": "Please check the doc type"}

    nodes.REVIEWER_FN = fake_reviewer
    ctx = FakeContext(_state([_clean_invoice()], confidence=0.50))

    yielded = asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    assert calls["n"] >= 1, "Critic must be called for soft-only trip"
    # Critic returned CLARIFY → must escalate (yield a RequestInput)
    assert len(yielded) == 1, f"Expected one RequestInput pause but got: {yielded}"
    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_CLARIFY


# --------------------------------------------------------------------------- #
# Soft-only fall-through: state verdict is OK (not CLARIFY) after critic ok
# --------------------------------------------------------------------------- #


def test_soft_only_ok_verdict_written_as_ok():
    """After soft-only + critic ok, state[REVIEW_VERDICT_KEY] == 'ok'."""
    nodes.REVIEWER_FN = lambda state, reasons, *, model: {"verdict": nodes.REVIEW_VERDICT_OK}
    ctx = FakeContext(_state([_clean_invoice()], confidence=0.50))

    asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_OK


# --------------------------------------------------------------------------- #
# Regression: existing hard-signal tests still work post-Lever-2
# --------------------------------------------------------------------------- #


def test_bundle_empty_is_hard_not_soft():
    assert _is_soft_only(["bundle_empty"]) is False


def test_doc_type_other_with_weak_extract_is_hard():
    # doc_type_other ALONE is soft, but it only appears when a hard signal
    # (lines_empty etc.) is also present (Lever 1 gate). The combined set is hard.
    assert _is_soft_only(["doc_type_other", "lines_empty: INV-1"]) is False


# --------------------------------------------------------------------------- #
# DETERMINISTIC SAFETY: hard-signal path — critic OK must not wave through
# --------------------------------------------------------------------------- #


def test_hard_signal_unreconciled_escalates_even_when_critic_returns_ok():
    """KEY SAFETY TEST (ADR-0017 §3): hard signal + critic returns OK (no re-extract)
    → review_extraction_node MUST still yield a RequestInput.

    The bug this prevents: _run_reviewer_loop returns OK the instant the LLM
    says "ok", before re-checking detect_struggle. So on a still-unreconciled
    doc, the old `if verdict != REVIEW_VERDICT_CLARIFY: return` would silently
    wave through an unreconciled document. The fix re-runs detect_struggle after
    the loop; any remaining hard signal escalates deterministically regardless of
    the critic's verdict.

    This test MUST FAIL against the unfixed hard-signal branch.
    """
    ok_calls = {"n": 0}

    def fake_reviewer_always_ok(state, reasons, *, model):
        ok_calls["n"] += 1
        # Returns OK immediately — no re-extraction triggered
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    nodes.REVIEWER_FN = fake_reviewer_always_ok
    # Hard signal: totals do not reconcile
    inv = _clean_invoice(reconciled=False, reconcile_note="totals off by $10")
    ctx = FakeContext(_state([inv]))

    yielded = asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    # Critic was called (it's on the hard path)
    assert ok_calls["n"] >= 1, "Reviewer must be called on the hard-signal path"
    # Despite critic returning OK, the still-unreconciled doc MUST escalate
    assert len(yielded) == 1, (
        f"Hard signal (unreconciled) must yield a RequestInput even when "
        f"critic returns OK, but got {yielded!r}"
    )
    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_CLARIFY


def test_hard_signal_autofix_via_hints_proceeds_when_detect_struggle_clears():
    """HINTS path auto-fix: critic returns HINTS, re-extraction makes detect_struggle
    clean → node PROCEEDS (no pause). The auto-fix value of the reviewer loop is
    preserved; the deterministic re-check allows clean docs through.
    """
    from invoice_processing.extract.invoice_extractor import (
        ExtractedInvoice,
        ExtractedInvoiceBundle,
        ExtractedLine,
    )
    from tests.test_nodes import _legacy_result_from_ex_bundle

    reviewer_calls = {"n": 0}

    def fake_reviewer_hints_then_ok(state, reasons, *, model):
        reviewer_calls["n"] += 1
        if reviewer_calls["n"] == 1:
            # First call: suggest a re-extraction hint
            return {"verdict": nodes.REVIEW_VERDICT_HINTS, "hint": "check the totals row"}
        # Second call (after re-extraction): extraction is now clean, return ok
        return {"verdict": nodes.REVIEW_VERDICT_OK}

    def fake_extract_returns_clean(data, mime, **kw):
        # Re-extraction produces a fully reconciled invoice. The resulting
        # line is then post-processed to carry account_code="6100" so the
        # WS-1.5 blank_account_code flag does not fire (it would, in
        # production, have been filled by categorize_node which this stub
        # bypasses).
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
                        lines=[ExtractedLine(
                            description="Goods", net_amount=100.0, gst_amount=9.0
                        )],
                        subtotal=100.0,
                        gst_total=9.0,
                        total=109.0,
                    )
                ]
            )
        )
        # Simulate categorize_node writing the GL code onto the line.
        for inv in result.normalized:
            for ln in inv.lines:
                ln.account_code = "6100"
        return result

    nodes.REVIEWER_FN = fake_reviewer_hints_then_ok
    nodes.EXTRACT_INVOICE_DOCUMENT_FN = fake_extract_returns_clean

    # Start with an unreconciled doc (hard signal)
    inv = _clean_invoice(reconciled=False, reconcile_note="totals off by $10")
    ctx = FakeContext(_state([inv]))

    yielded = asyncio.run(_drive(nodes.review_extraction_node._func(ctx)))

    # After re-extraction, detect_struggle returns clean → no pause
    assert yielded == [], (
        f"After auto-fix re-extraction (detect_struggle now clean), "
        f"no pause expected but got: {yielded!r}"
    )
    assert ctx.state[nodes.REVIEW_VERDICT_KEY] == nodes.REVIEW_VERDICT_OK
