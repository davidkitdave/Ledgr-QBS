"""Pure-signal tests for ``detect_struggle`` — the deterministic, ZERO-LLM
struggle detector that gates the extract reviewer.

Each of the six signals is shown to trip / not-trip independently, a clean
extraction returns ``(False, [])`` (proving the zero-LLM happy path never even
reaches ``REVIEWER_FN``), and the §0.5-C guard is pinned: a non-registered
client's invoice with missing GST is NORMAL, not a struggle.
"""

from __future__ import annotations

from accounting_agents import nodes
from accounting_agents.nodes import detect_struggle
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice


def _state(invoices=None, *, doc_type="invoice", confidence=0.95, **extra) -> dict:
    inv_dicts = [nodes._inv_to_dict(i) for i in (invoices or [])]
    state = {
        nodes.NORMALIZED_KEY: inv_dicts,
        nodes.DOC_TYPE_KEY: doc_type,
        nodes.CLASSIFY_CONFIDENCE_KEY: confidence,
        # WS-1.5: pre-set tax_jurisdiction so the jurisdiction_unresolved
        # flag does NOT fire by default. Tests that want to assert that
        # flag specifically override this with tax_jurisdiction=None.
        nodes.TAX_JURISDICTION_KEY: "SINGAPORE",
    }
    state.update(extra)
    return state


def _clean_invoice(**overrides) -> NormalizedInvoice:
    defaults = dict(
        invoice_number="INV-1",
        invoice_date=__import__("datetime").date(2025, 1, 15),
        doc_total=109.0,
        reconciled=True,
        our_gst_registered=True,
        lines=[InvoiceLine(description="Goods", net_amount=100.0, gst_amount=9.0,
                            account_code="6100")],
    )
    defaults.update(overrides)
    return NormalizedInvoice(**defaults)


# --------------------------------------------------------------------------- #
# Happy path: a clean extraction trips NOTHING (zero-LLM proof).
# --------------------------------------------------------------------------- #


def test_clean_invoice_does_not_trip():
    tripped, reasons = detect_struggle(_state([_clean_invoice()]))
    assert tripped is False
    assert reasons == []


# --------------------------------------------------------------------------- #
# Each signal trips independently.
# --------------------------------------------------------------------------- #


def test_signal_bundle_empty():
    tripped, reasons = detect_struggle(_state([]))
    assert tripped is True
    assert any(r == "bundle_empty" for r in reasons)


def test_signal_lines_empty():
    tripped, reasons = detect_struggle(_state([_clean_invoice(lines=[])]))
    assert tripped is True
    assert any(r.startswith("lines_empty") for r in reasons)


def test_signal_unreconciled():
    inv = _clean_invoice(reconciled=False, reconcile_note="subtotal mismatch")
    tripped, reasons = detect_struggle(_state([inv]))
    assert tripped is True
    note = next(r for r in reasons if r.startswith("unreconciled"))
    assert "subtotal mismatch" in note


def test_signal_doc_type_other_with_weak_extract_trips():
    # Lever 1: doc_type_other only trips when a weak-extract signal is also present.
    # A clean, reconciled 'other' no longer escalates (see ADR-0017).
    inv = _clean_invoice(lines=[])  # lines_empty is a weak-extract signal
    tripped, reasons = detect_struggle(_state([inv], doc_type="other"))
    assert tripped is True
    assert "doc_type_other" in reasons


def test_signal_doc_type_other_clean_no_trip():
    # Lever 1: a clean, reconciled 'other' must NOT trip (core quality gate).
    tripped, reasons = detect_struggle(_state([_clean_invoice()], doc_type="other"))
    assert tripped is False
    assert "doc_type_other" not in reasons


def test_signal_low_classify_confidence():
    tripped, reasons = detect_struggle(_state([_clean_invoice()], confidence=0.55))
    assert tripped is True
    assert "low_classify_confidence" in reasons


def test_signal_at_confidence_floor_does_not_trip():
    # Strictly-below the floor trips; exactly at the floor does not.
    tripped, reasons = detect_struggle(_state([_clean_invoice()], confidence=0.60))
    assert "low_classify_confidence" not in reasons
    assert tripped is False


def test_signal_missing_required_invoice_number():
    tripped, reasons = detect_struggle(_state([_clean_invoice(invoice_number=None)]))
    assert tripped is True
    note = next(r for r in reasons if r.startswith("missing_required"))
    assert "invoice_number" in note


def test_signal_missing_required_invoice_date():
    tripped, reasons = detect_struggle(_state([_clean_invoice(invoice_date=None)]))
    assert tripped is True
    note = next(r for r in reasons if r.startswith("missing_required"))
    assert "invoice_date" in note


def test_signal_missing_required_doc_total():
    tripped, reasons = detect_struggle(_state([_clean_invoice(doc_total=None)]))
    assert tripped is True
    note = next(r for r in reasons if r.startswith("missing_required"))
    assert "doc_total" in note


# --------------------------------------------------------------------------- #
# §0.5-C guard: a non-registered client's missing GST is NORMAL, not a struggle.
# --------------------------------------------------------------------------- #


def test_non_registered_missing_gst_does_not_trip():
    # Non-registered client: no GST on the line, no GST-inclusive doc_total —
    # this is legitimate, so the reviewer must NOT trip.
    inv = _clean_invoice(
        our_gst_registered=False,
        doc_total=None,  # would trip for a registered client; skipped here
        lines=[InvoiceLine(description="Service", net_amount=100.0, gst_amount=None,
                            account_code="6200")],
    )
    tripped, reasons = detect_struggle(_state([inv]))
    assert tripped is False
    assert reasons == []


# ---------------------------------------------------------------------------
# Terminal-gate (_needs_review) parity tests.
#
# detect_struggle runs at lane position 2 (review_extraction) — BEFORE
# categorize writes account_code and resolve_jurisdiction writes
# tax_jurisdiction. So the four WS-1.5 signals (blank_account_code,
# account_code_not_in_coa, jurisdiction_unresolved, currency_mismatch) read
# always-empty state there and over-flagged every commercial doc. They have
# been removed from detect_struggle; the genuine conditions are now caught
# post-resolution at the terminal approval gate in _needs_review. These tests
# prove the terminal gate still blocks export for each genuine condition, and
# the lane-order regression test locks the premature signals out of
# detect_struggle.
# ---------------------------------------------------------------------------


class TestTerminalNeedsReview:
    def _clean_invoice_with_account(self, **overrides) -> NormalizedInvoice:
        defaults = dict(
            invoice_number="INV-1",
            invoice_date=__import__("datetime").date(2025, 1, 15),
            doc_total=109.0,
            reconciled=True,
            our_gst_registered=True,
            currency="SGD",
            lines=[InvoiceLine(
                description="Goods", net_amount=100.0, gst_amount=9.0,
                account_code="6100",
            )],
        )
        defaults.update(overrides)
        return NormalizedInvoice(**defaults)

    def test_needs_review_currency_mismatch_my_sgd(self):
        """MY-jurisdiction invoice with currency=SGD → terminal gate flags."""
        inv = self._clean_invoice_with_account(currency="SGD")
        state = _state([inv])
        state[nodes.TAX_JURISDICTION_KEY] = "MALAYSIA"
        needs, reasons = nodes._needs_review(state)
        assert needs is True
        assert any("currency_mismatch" in r for r in reasons), reasons

    def test_needs_review_no_currency_mismatch_when_myr(self):
        """MY-jurisdiction invoice with currency=MYR → no currency reason."""
        inv = self._clean_invoice_with_account(currency="MYR")
        state = _state([inv])
        state[nodes.TAX_JURISDICTION_KEY] = "MALAYSIA"
        _needs, reasons = nodes._needs_review(state)
        assert not any("currency_mismatch" in r for r in reasons), reasons

    def test_needs_review_flagged_account_code(self):
        """A line with account_flagged=True (blank/abstained code) → caught."""
        inv = self._clean_invoice_with_account(
            lines=[InvoiceLine(
                description="Goods", net_amount=100.0, gst_amount=9.0,
                account_code="", account_flagged=True,
                account_flag_reason="unresolved",
            )],
        )
        state = _state([inv])
        needs, reasons = nodes._needs_review(state)
        assert needs is True
        assert any("flagged for account review" in r for r in reasons), reasons

    def test_needs_review_ambiguous_jurisdiction(self):
        """AMBIGUOUS jurisdiction + flag_for_human → terminal gate flags."""
        inv = self._clean_invoice_with_account()
        state = _state([inv])
        state[nodes.TAX_JURISDICTION_KEY] = nodes.JURISDICTION_AMBIGUOUS
        state[nodes.FLAG_FOR_HUMAN_KEY] = True
        needs, reasons = nodes._needs_review(state)
        assert needs is True
        assert any("region" in r.lower() for r in reasons), reasons


class TestLaneOrderRegression:
    """Lock the four removed premature signals out of detect_struggle.

    detect_struggle runs before categorize/resolve_jurisdiction, so a clean
    commercial invoice with an EMPTY account_code and NO tax_jurisdiction in
    state must NOT emit any of the four removed signals. This pins the fix
    against re-introduction.
    """

    def test_detect_struggle_omits_premature_signals(self):
        inv = NormalizedInvoice(
            invoice_number="INV-1",
            invoice_date=__import__("datetime").date(2025, 1, 15),
            doc_total=109.0,
            reconciled=True,
            our_gst_registered=True,
            currency="SGD",
            lines=[InvoiceLine(
                description="Goods", net_amount=100.0, gst_amount=9.0,
                account_code="",  # not yet categorized at this lane position
            )],
        )
        state = {
            nodes.NORMALIZED_KEY: [nodes._inv_to_dict(inv)],
            nodes.DOC_TYPE_KEY: "invoice",
            nodes.CLASSIFY_CONFIDENCE_KEY: 0.95,
            # No tax_jurisdiction set — resolve_jurisdiction hasn't run yet.
        }
        _tripped, reasons = detect_struggle(state)
        for forbidden in (
            "blank_account_code",
            "account_code_not_in_coa",
            "jurisdiction_unresolved",
            "currency_mismatch",
        ):
            assert not any(forbidden in r for r in reasons), (
                f"{forbidden} must not be emitted by detect_struggle. Got: {reasons}"
            )
