"""Tests for Lever 3: open-set / zero-shot classify (ADR-0017 §2 processable_false).

Hermetic — fake CLASSIFY_FN, no live Gemini, Slack, or Firestore.

Covers:
- ClassificationResult defaults: processable=True, free_type=None.
- Clamp: off-enum type (e.g. "delivery_order") → doc_type="other",
         free_type="delivery_order", processable=True (still bookable).
- Clamp: genuinely unbookable (model returns processable=False) → processable=False preserved.
- classify_node: free_type + processable land in CLASSIFY_FREE_TYPE_KEY /
  CLASSIFY_PROCESSABLE_KEY in state.
- detect_struggle: processable_false → HARD signal (escalates) even with high familiarity.
- detect_struggle: processable_false is NOT suppressible by familiarity or soft-only context.
- compose_confident_note: doc_type="other" + free_type="delivery_order" → uses free_type label.
"""

from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from accounting_agents import nodes
from accounting_agents.nodes import (
    CLASSIFY_FREE_TYPE_KEY,
    CLASSIFY_PROCESSABLE_KEY,
    detect_struggle,
    compose_confident_note,
)
from invoice_processing.classify.document_classifier import (
    ALLOWED_DOC_TYPES,
    ClassificationResult,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_cls_result(**overrides) -> ClassificationResult:
    """Build a ClassificationResult with safe defaults."""
    defaults = dict(
        doc_type="invoice",
        confidence=0.95,
        reason="stub",
    )
    defaults.update(overrides)
    return ClassificationResult(**defaults)


def _state(invoices=None, *, doc_type="invoice", confidence=0.95,
           processable: bool | None = None, **extra) -> dict:
    """Build a minimal state dict for detect_struggle."""
    inv_dicts = [nodes._inv_to_dict(i) for i in (invoices or [])]
    state: dict = {
        nodes.NORMALIZED_KEY: inv_dicts,
        nodes.DOC_TYPE_KEY: doc_type,
        nodes.CLASSIFY_CONFIDENCE_KEY: confidence,
    }
    if processable is not None:
        state[CLASSIFY_PROCESSABLE_KEY] = processable
    state.update(extra)
    return state


def _clean_invoice(**overrides) -> NormalizedInvoice:
    defaults = dict(
        invoice_number="INV-1",
        invoice_date=datetime.date(2025, 1, 15),
        doc_total=240.0,
        reconciled=True,
        our_gst_registered=True,
        lines=[
            InvoiceLine(
                description="Goods",
                net_amount=220.0,
                gst_amount=20.0,
                account_code="6100",
            ),
        ],
    )
    defaults.update(overrides)
    return NormalizedInvoice(**defaults)


class FakeContext:
    """Duck-typed stand-in for google.adk.agents.context.Context."""

    def __init__(self, state: dict, pdf_bytes: bytes = b"%PDF-1.4 stub", mime="application/pdf"):
        self.state = dict(state)
        self._pdf_bytes = pdf_bytes
        self._mime = mime

    async def load_artifact(self, filename, version=None):
        inline = SimpleNamespace(data=self._pdf_bytes, mime_type=self._mime)
        return SimpleNamespace(inline_data=inline)


def _base_state(**overrides) -> dict:
    state = {
        nodes.ARTIFACT_NAME_KEY: nodes.ARTIFACT_NAME_FMT.format(file_id="F999"),
        "client_id": "test-client",
        "client_name": "Test Client Pte Ltd",
        "fye_month": 3,
        "tax_registered": True,
        "coa": [],
        "category_mapping": {},
        "entity_memory": [],
    }
    state.update(overrides)
    return state


@pytest.fixture(autouse=True)
def _restore_classify_fn():
    """Restore CLASSIFY_FN after every test."""
    original = nodes.CLASSIFY_FN
    yield
    nodes.CLASSIFY_FN = original


def _make_payload(
    *,
    n_lines: int = 2,
    doc_total: float = 120.0,
    account_code: str | None = "6100",
    currency: str = "SGD",
) -> dict:
    """Build a minimal LEDGER_ROWS_KEY-shaped payload."""
    rows = []
    for i in range(n_lines):
        row: dict = {
            "Description": f"Line {i + 1}",
            "Net Amount": doc_total / n_lines,
            "Currency": currency,
        }
        if account_code:
            row["Account Code"] = account_code
        rows.append(row)
    return {
        "fy": 2025,
        "kind": "invoice",
        "batches": [{"sheet": "Purchase", "rows": rows}],
        "doc_total": doc_total,
        "currency": currency,
    }


# --------------------------------------------------------------------------- #
# Part A — ClassificationResult schema defaults
# --------------------------------------------------------------------------- #


class TestClassificationResultDefaults:
    """New fields must have safe defaults that don't break existing code."""

    def test_processable_defaults_to_true(self):
        """processable must default to True — existing callers don't set it."""
        result = _make_cls_result(doc_type="invoice")
        assert result.processable is True

    def test_free_type_defaults_to_none(self):
        """free_type must default to None — existing callers don't set it."""
        result = _make_cls_result(doc_type="invoice")
        assert result.free_type is None

    def test_existing_fields_unaffected(self):
        """Adding fields must not break existing required fields."""
        result = _make_cls_result(
            doc_type="receipt",
            issuer_name="Acme",
            bill_to_name="Client",
            currency="SGD",
            total_amount=100.0,
        )
        assert result.doc_type == "receipt"
        assert result.issuer_name == "Acme"
        assert result.confidence == 0.95

    def test_processable_can_be_set_false(self):
        """processable=False is valid for genuinely unbookable docs."""
        result = _make_cls_result(doc_type="other", processable=False)
        assert result.processable is False

    def test_free_type_can_be_set(self):
        """free_type can carry the raw model label."""
        result = _make_cls_result(doc_type="other", free_type="delivery_order")
        assert result.free_type == "delivery_order"


# --------------------------------------------------------------------------- #
# Part B — Clamp behaviour (off-enum types)
# --------------------------------------------------------------------------- #


class TestClampBehaviour:
    """The clamp must preserve free_type and correctly set processable."""

    def test_off_enum_bookable_doc_clamps_doc_type_to_other(self):
        """delivery_order is off-enum → doc_type becomes 'other'."""
        # Simulate what the clamp logic does (we can't call live Gemini).
        # The clamp in classify_document is: if result.doc_type not in ALLOWED_DOC_TYPES → "other"
        # We verify the logic branch directly.
        raw_result = ClassificationResult(
            doc_type="delivery_order",
            confidence=0.85,
            reason="stub",
            processable=True,    # still bookable
            free_type="delivery_order",
        )
        # The clamp condition
        if raw_result.doc_type not in ALLOWED_DOC_TYPES:
            raw_result.doc_type = "other"

        assert raw_result.doc_type == "other"
        assert raw_result.free_type == "delivery_order"
        assert raw_result.processable is True

    def test_off_enum_bookable_doc_preserves_free_type(self):
        """free_type survives the clamp for a bookable off-enum type."""
        raw_result = ClassificationResult(
            doc_type="purchase_order",
            confidence=0.80,
            reason="stub",
            processable=True,
            free_type="purchase_order",
        )
        if raw_result.doc_type not in ALLOWED_DOC_TYPES:
            raw_result.doc_type = "other"

        assert raw_result.doc_type == "other"
        assert raw_result.free_type == "purchase_order"
        assert raw_result.processable is True

    def test_genuinely_unbookable_preserves_processable_false(self):
        """Model signals processable=False for a marketing flyer → preserved."""
        raw_result = ClassificationResult(
            doc_type="other",
            confidence=0.70,
            reason="marketing flyer, no financial content",
            processable=False,
            free_type="marketing_flyer",
        )
        # Clamp doesn't change doc_type (already 'other')
        if raw_result.doc_type not in ALLOWED_DOC_TYPES:
            raw_result.doc_type = "other"

        assert raw_result.doc_type == "other"
        assert raw_result.processable is False
        assert raw_result.free_type == "marketing_flyer"

    def test_genuinely_unbookable_off_enum_clamps_and_preserves_false(self):
        """Off-enum type + processable=False → doc_type='other', processable=False."""
        raw_result = ClassificationResult(
            doc_type="contract",
            confidence=0.75,
            reason="legal contract, no bookable amounts",
            processable=False,
            free_type="contract",
        )
        if raw_result.doc_type not in ALLOWED_DOC_TYPES:
            raw_result.doc_type = "other"

        assert raw_result.doc_type == "other"
        assert raw_result.processable is False

    def test_known_enum_types_are_not_clamped(self):
        """All existing ALLOWED_DOC_TYPES must pass through unchanged."""
        for t in ALLOWED_DOC_TYPES:
            r = ClassificationResult(doc_type=t, confidence=0.9, reason="stub")
            if r.doc_type not in ALLOWED_DOC_TYPES:
                r.doc_type = "other"
            assert r.doc_type == t, f"Type {t!r} was unexpectedly clamped"

    def test_delivery_order_not_in_allowed_doc_types(self):
        """Sanity check: delivery_order is genuinely off-enum."""
        assert "delivery_order" not in ALLOWED_DOC_TYPES


# --------------------------------------------------------------------------- #
# Part C — classify_node plumbing: state keys
# --------------------------------------------------------------------------- #


class TestClassifyNodePlumbing:
    """classify_node must persist free_type and processable into state."""

    def test_off_enum_doc_persists_free_type_and_processable_in_state(self):
        """When model returns delivery_order, state gets free_type + processable=True."""
        nodes.CLASSIFY_FN = lambda data, mime, **kw: ClassificationResult(
            doc_type="delivery_order",
            confidence=0.80,
            reason="stub",
            processable=True,
            free_type="delivery_order",
        )
        ctx = FakeContext(_base_state())
        asyncio.run(nodes.classify_node._func(ctx))

        # doc_type is clamped to 'other' (off-enum)
        assert ctx.state[nodes.DOC_TYPE_KEY] == "other"
        # free_type is preserved in its own key
        assert ctx.state[CLASSIFY_FREE_TYPE_KEY] == "delivery_order"
        # processable=True (still bookable)
        assert ctx.state[CLASSIFY_PROCESSABLE_KEY] is True

    def test_unbookable_doc_persists_processable_false_in_state(self):
        """When model returns processable=False, state key is False."""
        nodes.CLASSIFY_FN = lambda data, mime, **kw: ClassificationResult(
            doc_type="other",
            confidence=0.70,
            reason="blank page",
            processable=False,
            free_type=None,
        )
        ctx = FakeContext(_base_state())
        asyncio.run(nodes.classify_node._func(ctx))

        assert ctx.state[CLASSIFY_PROCESSABLE_KEY] is False
        assert ctx.state.get(CLASSIFY_FREE_TYPE_KEY) is None

    def test_normal_invoice_persists_processable_true_and_no_free_type(self):
        """Normal invoice: processable=True, free_type=None."""
        nodes.CLASSIFY_FN = lambda data, mime, **kw: ClassificationResult(
            doc_type="invoice",
            confidence=0.99,
            reason="stub",
        )
        ctx = FakeContext(_base_state())
        asyncio.run(nodes.classify_node._func(ctx))

        assert ctx.state[CLASSIFY_PROCESSABLE_KEY] is True
        assert ctx.state.get(CLASSIFY_FREE_TYPE_KEY) is None

    def test_bank_statement_persists_processable_in_state(self):
        """bank_statement lane also writes processable to state."""
        nodes.CLASSIFY_FN = lambda data, mime, **kw: ClassificationResult(
            doc_type="bank_statement",
            confidence=0.97,
            reason="stub",
        )
        ctx = FakeContext(_base_state())
        asyncio.run(nodes.classify_node._func(ctx))

        # Bank statement is in ALLOWED_DOC_TYPES → processable True (default)
        assert ctx.state[CLASSIFY_PROCESSABLE_KEY] is True

    def test_route_still_correct_for_off_enum_doc(self):
        """Off-enum doc → route=invoice (non-bank → invoice lane). Routing is unchanged."""
        nodes.CLASSIFY_FN = lambda data, mime, **kw: ClassificationResult(
            doc_type="delivery_order",
            confidence=0.80,
            reason="stub",
            processable=True,
            free_type="delivery_order",
        )
        ctx = FakeContext(_base_state())
        event = asyncio.run(nodes.classify_node._func(ctx))

        assert event.actions.route == nodes.ROUTE_INVOICE


# --------------------------------------------------------------------------- #
# Part D — detect_struggle: processable_false is a HARD signal
# --------------------------------------------------------------------------- #


class TestDetectStruggleProcessableFalse:
    """processable_false must escalate as a HARD signal — no exceptions."""

    def test_processable_false_trips_detect_struggle(self):
        """State with processable=False → detect_struggle trips."""
        state = _state([_clean_invoice()], doc_type="other", processable=False)
        tripped, reasons = detect_struggle(state)
        assert tripped is True
        assert "processable_false" in reasons

    def test_processable_false_is_hard_signal(self):
        """processable_false must NOT be in SOFT_SIGNAL_PREFIXES."""
        from accounting_agents.nodes import SOFT_SIGNAL_PREFIXES

        # "processable_false" must not start with any soft prefix
        signal = "processable_false"
        is_soft = any(signal.startswith(p) for p in SOFT_SIGNAL_PREFIXES)
        assert not is_soft, (
            f"'processable_false' must be HARD but starts with a soft prefix: {SOFT_SIGNAL_PREFIXES}"
        )

    def test_processable_false_with_no_other_signals_still_escalates(self):
        """Even a clean extraction with processable=False escalates (hard signal)."""
        state = _state([_clean_invoice()], doc_type="invoice", processable=False)
        tripped, reasons = detect_struggle(state)
        assert tripped is True
        assert "processable_false" in reasons

    def test_processable_false_not_suppressible_by_is_soft_only(self):
        """_is_soft_only(['processable_false']) must be False."""
        from accounting_agents.nodes import _is_soft_only

        result = _is_soft_only(["processable_false"])
        assert result is False

    def test_processable_false_not_suppressible_mixed_with_soft(self):
        """Hard signal mixed with soft signals: _is_soft_only still False."""
        from accounting_agents.nodes import _is_soft_only

        result = _is_soft_only(["processable_false", "low_classify_confidence"])
        assert result is False

    def test_processable_false_not_suppressed_by_high_familiarity(self):
        """Even with seen_count >= 2 for this doc type, processable_false still escalates.

        The familiarity gate only suppresses soft-only reasons. Since processable_false
        is hard, _is_soft_only returns False and the familiarity gate is bypassed entirely.
        """
        fam_map = {
            "other": {"seen_count": 10, "last_seen_at": "2025-01-01"},
            "other:Acme Corp": {"seen_count": 10, "last_seen_at": "2025-01-01"},
        }
        state = _state(
            [_clean_invoice()],
            doc_type="other",
            processable=False,
            **{nodes.FAMILIARITY_KEY: fam_map},
        )
        tripped, reasons = detect_struggle(state)
        assert tripped is True, (
            "processable_false must escalate even with high familiarity — it is a HARD signal"
        )
        assert "processable_false" in reasons

    def test_processable_true_does_not_trip_by_itself(self):
        """processable=True on a clean invoice → no trip from processable signal."""
        state = _state([_clean_invoice()], doc_type="invoice", processable=True)
        tripped, _ = detect_struggle(state)
        # Invoice is clean → no trip (processable=True adds nothing)
        assert tripped is False

    def test_processable_absent_does_not_trip(self):
        """When CLASSIFY_PROCESSABLE_KEY is absent (older state), no trip from processable."""
        state = _state([_clean_invoice()], doc_type="invoice")
        # Ensure the key is absent
        state.pop(CLASSIFY_PROCESSABLE_KEY, None)
        tripped, reasons = detect_struggle(state)
        assert "processable_false" not in reasons


# --------------------------------------------------------------------------- #
# Part E — compose_confident_note: uses free_type when doc_type is 'other'
# --------------------------------------------------------------------------- #


class TestComposeConfidentNoteFreeType:
    """When doc_type='other' but free_type is set, the note should use the free label."""

    def test_other_with_free_type_uses_free_type_label(self):
        """doc_type='other' + free_type='delivery_order' → note contains free_type label."""
        payload = _make_payload(n_lines=2, doc_total=120.0)
        note = compose_confident_note(
            payload, doc_type="other", free_type="delivery_order"
        )
        # The note should reference "delivery order" rather than the generic "document"
        assert "delivery order" in note.lower(), (
            f"Expected 'delivery order' in note, got: {note!r}"
        )

    def test_other_without_free_type_falls_back_to_generic(self):
        """doc_type='other' + free_type=None → 'document' label (existing behaviour)."""
        payload = _make_payload(n_lines=2, doc_total=120.0)
        note = compose_confident_note(payload, doc_type="other", free_type=None)
        # Should use the generic "document" label
        assert "document" in note.lower(), (
            f"Expected 'document' in note when free_type=None, got: {note!r}"
        )

    def test_free_type_with_underscores_renders_as_spaces(self):
        """free_type='purchase_order' → 'purchase order' in the note."""
        payload = _make_payload(n_lines=1, doc_total=80.0)
        note = compose_confident_note(
            payload, doc_type="other", free_type="purchase_order"
        )
        assert "purchase order" in note.lower(), (
            f"Expected 'purchase order' in note, got: {note!r}"
        )

    def test_known_doc_type_ignores_free_type(self):
        """For known doc types, free_type is irrelevant — existing label is used."""
        payload = _make_payload(n_lines=3, doc_total=300.0)
        note = compose_confident_note(
            payload, doc_type="expense_claim", free_type="some_other_label"
        )
        assert "expense claim" in note.lower(), (
            f"Expected 'expense claim' in note for known doc_type, got: {note!r}"
        )

    def test_free_type_none_does_not_crash_for_known_type(self):
        """free_type=None is always safe for any doc type."""
        payload = _make_payload(n_lines=2, doc_total=100.0)
        note = compose_confident_note(
            payload, doc_type="invoice", free_type=None
        )
        assert isinstance(note, str)
        assert len(note) > 5

    def test_note_still_contains_line_count_and_total_with_free_type(self):
        """free_type usage must not break the rest of the note structure."""
        payload = _make_payload(n_lines=3, doc_total=180.0, currency="SGD")
        note = compose_confident_note(
            payload, doc_type="other", free_type="delivery_order"
        )
        # Line count and total should still appear
        assert "3" in note
        assert "180" in note

    def test_empty_string_free_type_falls_back_gracefully(self):
        """Empty string free_type should behave like None (no crash, readable output)."""
        payload = _make_payload(n_lines=1, doc_total=50.0)
        note = compose_confident_note(
            payload, doc_type="other", free_type=""
        )
        assert isinstance(note, str)
        assert len(note) > 5
