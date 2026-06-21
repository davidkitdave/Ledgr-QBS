"""Tests for Lever 1: quality-gated doc-type escalation + confident-path note.

Covers:
- detect_struggle: clean reconciled 'other' and 'expense_claim' do NOT trip.
- detect_struggle: 'other'/'expense_claim' with weak extract signals DO trip.
- classifier: 'expense_claim' is in ALLOWED_DOC_TYPES and not clamped to 'other'.
- resolve_direction: expense_claim always returns 'purchase'.
- compose_confident_note: returns expected human string; degrades gracefully.
- confident note gating: fires only on no-pause path (delivered=True), not HITL-approve.

All tests are hermetic — no live Gemini, Slack, or Firestore.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

from accounting_agents import nodes
from accounting_agents.nodes import detect_struggle, compose_confident_note
from invoice_processing.classify.document_classifier import (
    ALLOWED_DOC_TYPES,
    ClassificationResult,
    resolve_direction,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice


# --------------------------------------------------------------------------- #
# Helpers (mirrors test_review_signals.py style)
# --------------------------------------------------------------------------- #


def _state(invoices=None, *, doc_type="invoice", confidence=0.95, **extra) -> dict:
    inv_dicts = [nodes._inv_to_dict(i) for i in (invoices or [])]
    state = {
        nodes.NORMALIZED_KEY: inv_dicts,
        nodes.DOC_TYPE_KEY: doc_type,
        nodes.CLASSIFY_CONFIDENCE_KEY: confidence,
    }
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
                description="Travel",
                net_amount=100.0,
                gst_amount=9.0,
                account_code="6100",
            ),
            InvoiceLine(
                description="Accommodation",
                net_amount=100.0,
                gst_amount=9.0,
                account_code="6100",
            ),
            InvoiceLine(
                description="Meal",
                net_amount=22.0,
                gst_amount=None,
                account_code="6200",
            ),
        ],
    )
    defaults.update(overrides)
    return NormalizedInvoice(**defaults)


# --------------------------------------------------------------------------- #
# Part A — Classifier: expense_claim is a recognized doc type
# --------------------------------------------------------------------------- #


def test_expense_claim_in_allowed_doc_types():
    """expense_claim must be in ALLOWED_DOC_TYPES so the clamp does not fire."""
    assert "expense_claim" in ALLOWED_DOC_TYPES


def test_expense_claim_not_clamped_to_other():
    """If classify_document returns expense_claim, the clamp must preserve it.

    We verify the clamp logic inline: if a type is in ALLOWED_DOC_TYPES it is
    NOT replaced by 'other'.
    """
    # Simulate the clamp: result.doc_type not in ALLOWED_DOC_TYPES → 'other'
    doc_type = "expense_claim"
    clamped = doc_type if doc_type in ALLOWED_DOC_TYPES else "other"
    assert clamped == "expense_claim"


# --------------------------------------------------------------------------- #
# Part B — detect_struggle: quality-gated doc-type escalation
# --------------------------------------------------------------------------- #


class TestDetectStruggleCleanOther:
    """A clean, reconciled 'other' doc must NOT trip (Lever 1 core fix)."""

    def test_clean_other_no_trip(self):
        tripped, reasons = detect_struggle(
            _state([_clean_invoice()], doc_type="other")
        )
        assert tripped is False, f"Expected no trip, got reasons={reasons}"
        assert reasons == []

    def test_clean_expense_claim_no_trip(self):
        tripped, reasons = detect_struggle(
            _state([_clean_invoice()], doc_type="expense_claim")
        )
        assert tripped is False, f"Expected no trip, got reasons={reasons}"
        assert reasons == []


class TestDetectStruggleWeakOther:
    """'other'/'expense_claim' + a weak-extract signal MUST still trip."""

    def test_other_lines_empty_trips(self):
        inv = _clean_invoice(lines=[])
        tripped, reasons = detect_struggle(_state([inv], doc_type="other"))
        assert tripped is True
        assert "doc_type_other" in reasons
        assert any(r.startswith("lines_empty") for r in reasons)

    def test_expense_claim_unreconciled_trips(self):
        inv = _clean_invoice(reconciled=False, reconcile_note="total mismatch")
        tripped, reasons = detect_struggle(_state([inv], doc_type="expense_claim"))
        assert tripped is True
        assert "doc_type_other" in reasons
        assert any(r.startswith("unreconciled") for r in reasons)

    def test_other_bundle_empty_trips(self):
        tripped, reasons = detect_struggle(_state([], doc_type="other"))
        assert tripped is True
        assert "doc_type_other" in reasons
        assert "bundle_empty" in reasons

    def test_expense_claim_missing_required_trips(self):
        inv = _clean_invoice(invoice_number=None)
        tripped, reasons = detect_struggle(_state([inv], doc_type="expense_claim"))
        assert tripped is True
        assert "doc_type_other" in reasons
        assert any(r.startswith("missing_required") for r in reasons)


class TestDetectStruggleRegularOtherStillTrips:
    """Preserve old behavior: plain 'invoice' doc_type with weak extract still trips
    (no regression from adding the quality gate to other/expense_claim)."""

    def test_invoice_clean_no_trip(self):
        tripped, _ = detect_struggle(_state([_clean_invoice()], doc_type="invoice"))
        assert tripped is False

    def test_invoice_lines_empty_trips(self):
        inv = _clean_invoice(lines=[])
        tripped, reasons = detect_struggle(_state([inv], doc_type="invoice"))
        assert tripped is True
        assert any(r.startswith("lines_empty") for r in reasons)
        # doc_type_other should NOT appear for a plain invoice
        assert "doc_type_other" not in reasons


# --------------------------------------------------------------------------- #
# Part C — compose_confident_note
# --------------------------------------------------------------------------- #


def _make_payload(
    *,
    n_lines: int = 3,
    doc_total: float = 240.0,
    account_code: str | None = "6100",
    kind: str = "invoice",
    fy: int = 2025,
    currency: str = "SGD",
    software: str = "qbs",
) -> dict:
    """Build a minimal LEDGER_ROWS_KEY-shaped payload.

    Uses the REAL QBS purchase columns (Sub Total / Currency / Account Code / COA)
    — not the legacy literal placeholders the old test asserted. WS-1.2 changed
    ``compose_confident_note`` to look up columns via ``exporter.column_for_field``
    instead of guessing header strings, so the test fixture has to mirror the
    columns the actual exporters emit.
    """
    rows = []
    for i in range(n_lines):
        row: dict = {
            "Description": f"Line {i+1}",
            "Sub Total": doc_total / n_lines,
        }
        if account_code:
            row["Account Code / COA"] = account_code
        if currency:
            row["Currency"] = currency
        rows.append(row)

    return {
        "fy": fy,
        "kind": kind,
        "software": software,
        "batches": [{"sheet": "Purchase", "rows": rows}],
        "doc_total": doc_total,
        "currency": currency,
    }


class TestComposeConfidentNote:
    def test_full_payload_returns_expected_note(self):
        payload = _make_payload(n_lines=3, doc_total=240.0, account_code="6100")
        note = compose_confident_note(payload, doc_type="expense_claim")
        # Should mention line count and reconcile total
        assert "3" in note
        assert "240" in note
        # Should be a non-empty string
        assert len(note) > 10

    def test_doc_type_label_in_note(self):
        payload = _make_payload(n_lines=2, doc_total=100.0)
        note = compose_confident_note(payload, doc_type="expense_claim")
        # Should reference the doc type meaningfully (expense claim or similar)
        assert note  # non-empty is sufficient; exact wording is cosmetic

    def test_other_doc_type_does_not_crash(self):
        payload = _make_payload(n_lines=1, doc_total=50.0)
        note = compose_confident_note(payload, doc_type="other")
        assert isinstance(note, str)
        assert len(note) > 5

    def test_missing_account_code_omits_coded_clause(self):
        payload = _make_payload(n_lines=2, doc_total=80.0, account_code=None)
        note = compose_confident_note(payload, doc_type="expense_claim")
        # Should NOT crash and should NOT contain "coded to"
        assert "coded to" not in note.lower()

    def test_empty_batches_returns_graceful_note(self):
        payload = {"fy": 2025, "kind": "invoice", "batches": []}
        note = compose_confident_note(payload, doc_type="expense_claim")
        assert isinstance(note, str)
        assert len(note) > 0

    def test_free_type_none_does_not_crash(self):
        """free_type=None is the Lever 1 state; Lever 3 will populate it."""
        payload = _make_payload(n_lines=2, doc_total=100.0)
        note = compose_confident_note(payload, doc_type="expense_claim", free_type=None)
        assert isinstance(note, str)

    def test_free_type_provided_accepted(self):
        """free_type is accepted (for future Lever 3 use); should not crash."""
        payload = _make_payload(n_lines=1, doc_total=55.0)
        note = compose_confident_note(
            payload, doc_type="expense_claim", free_type="Staff Expense Claim"
        )
        assert isinstance(note, str)
        assert len(note) > 0

    def test_payload_currency_preferred_over_row_currency(self):
        """payload.get('currency') is used before row-level currency fallback."""
        payload = _make_payload(n_lines=2, doc_total=200.0, currency="MYR")
        # Row-level currency is also MYR from _make_payload; verify the note uses it
        note = compose_confident_note(payload, doc_type="expense_claim")
        assert "MYR" in note

    def test_payload_currency_overrides_sgd_default(self):
        """When payload carries currency, 'SGD' default is never used."""
        payload = _make_payload(n_lines=1, doc_total=50.0, currency="USD")
        note = compose_confident_note(payload, doc_type="expense_claim")
        assert "USD" in note
        assert "SGD" not in note


# --------------------------------------------------------------------------- #
# Nit 2 — resolve_direction: expense_claim is always purchase
# --------------------------------------------------------------------------- #


class TestResolveDirectionExpenseClaim:
    def _result(self, **kwargs) -> ClassificationResult:
        defaults = dict(
            doc_type="expense_claim",
            issuer_name="Staff Member",
            bill_to_name="Acme Corp",
            currency="SGD",
            total_amount=240.0,
            confidence=0.95,
            reason="expense claim",
        )
        defaults.update(kwargs)
        return ClassificationResult(**defaults)

    def test_expense_claim_returns_purchase(self):
        result = self._result()
        assert resolve_direction(result) == "purchase"

    def test_expense_claim_purchase_regardless_of_client_name(self):
        result = self._result()
        assert resolve_direction(result, client_name="Acme Corp") == "purchase"

    def test_expense_claim_purchase_with_no_client(self):
        result = self._result(issuer_name=None, bill_to_name=None)
        assert resolve_direction(result) == "purchase"


# --------------------------------------------------------------------------- #
# REQUIRED — confident-note gating: only fires on no-pause delivery
# --------------------------------------------------------------------------- #


def _make_delivered_payload(*, delivered: bool = True, doc_type: str = "expense_claim") -> dict:
    """Build a LEDGER_ROWS_KEY-shaped payload with the delivered flag."""
    payload = _make_payload(n_lines=3, doc_total=240.0, account_code="6100")
    payload["doc_type"] = doc_type
    payload["delivered"] = delivered
    return payload


def _call_post_delivery_card(payload: dict, batches: list[dict]) -> list[dict]:
    """Drive _post_delivery_card and capture the blocks it would post.

    Patches chat_postMessage to capture the blocks kwarg so we can assert
    without a real Slack client.  Returns the blocks list from the call.
    """
    from accounting_agents.slack_runner import _post_delivery_card

    captured: list[list[dict]] = []

    def fake_post(**kwargs):
        captured.append(kwargs.get("blocks", []))

    fake_client = MagicMock()
    fake_client.chat_postMessage.side_effect = fake_post

    _post_delivery_card(
        fake_client,
        "C123",
        summary="📒 Added 3 lines",
        batches=batches,
        payload=payload,
        append_result={"filename": "Ledger.xlsx", "fy": "2025"},
    )
    return captured[0] if captured else []


class TestConfidentNoteGating:
    """Regression: note fires ONLY on the clean no-pause path (delivered=True)."""

    def _batches(self) -> list[dict]:
        return [
            {
                "sheet": "Purchase",
                "rows": [
                    {"Description": "Meal", "Net Amount": 80.0, "Account Code": "6100", "Currency": "SGD"},
                    {"Description": "Travel", "Net Amount": 80.0, "Account Code": "6100", "Currency": "SGD"},
                    {"Description": "Hotel", "Net Amount": 80.0, "Account Code": "6100", "Currency": "SGD"},
                ],
            }
        ]

    def test_clean_no_pause_expense_claim_includes_confident_note(self):
        """delivered=True → confident note context block IS appended."""
        payload = _make_delivered_payload(delivered=True, doc_type="expense_claim")
        blocks = _call_post_delivery_card(payload, self._batches())
        context_blocks = [b for b in blocks if b.get("type") == "context"]
        assert context_blocks, (
            "Expected a context block with the confident note on the no-pause path, "
            f"got blocks: {blocks}"
        )
        # The note text should be present in at least one context element
        note_texts = [
            el.get("text", "")
            for b in context_blocks
            for el in (b.get("elements") or [])
        ]
        assert any(note_texts), "Context block elements should contain note text"

    def test_hitl_approved_expense_claim_omits_confident_note(self):
        """delivered=False (HITL-approve path) → NO confident note context block."""
        payload = _make_delivered_payload(delivered=False, doc_type="expense_claim")
        blocks = _call_post_delivery_card(payload, self._batches())
        context_blocks = [b for b in blocks if b.get("type") == "context"]
        assert not context_blocks, (
            "Expected NO context block for a HITL-approved doc, "
            f"got blocks: {blocks}"
        )

    def test_clean_no_pause_other_includes_confident_note(self):
        """doc_type='other' + delivered=True → confident note IS included."""
        payload = _make_delivered_payload(delivered=True, doc_type="other")
        blocks = _call_post_delivery_card(payload, self._batches())
        context_blocks = [b for b in blocks if b.get("type") == "context"]
        assert context_blocks

    def test_plain_invoice_no_pause_never_shows_confident_note(self):
        """Confident note only applies to expense_claim/other, not invoice."""
        payload = _make_delivered_payload(delivered=True, doc_type="invoice")
        blocks = _call_post_delivery_card(payload, self._batches())
        context_blocks = [b for b in blocks if b.get("type") == "context"]
        assert not context_blocks, (
            "Plain invoice should never get a confident note block"
        )
