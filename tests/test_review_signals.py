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
# WS-1.5 — runtime fail-loud flags. Each of the four new signals must trip
# the reviewer so HITL routes the doc to a human instead of silently
# booking bad data. The D1 critical path test: an MY doc with lost
# reference_yaml must flag (not silently SG-default).
# ---------------------------------------------------------------------------


class TestWS15RuntimeFlags:
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

    def test_jurisdiction_unresolved_when_tax_jurisdiction_missing(self):
        """D1 critical: a doc with no tax_jurisdiction must flag — the
        previous code silently SG-defaulted. Now HITL is forced."""
        inv = self._clean_invoice_with_account()
        state = _state([inv])
        # Wipe the default tax_jurisdiction (the helper pre-sets it).
        state[nodes.TAX_JURISDICTION_KEY] = None
        tripped, reasons = detect_struggle(state)
        assert tripped is True
        assert "jurisdiction_unresolved" in reasons

    def test_jurisdiction_unresolved_when_flag_for_human_set(self):
        """resolve_jurisdiction_node sets flag_for_human=True when the
        jurisdiction is ambiguous. detect_struggle must respect that flag."""
        inv = self._clean_invoice_with_account()
        state = _state([inv])
        state[nodes.FLAG_FOR_HUMAN_KEY] = True
        tripped, reasons = detect_struggle(state)
        assert tripped is True
        assert "jurisdiction_unresolved" in reasons

    def test_blank_account_code_flags_for_invoice_doc(self):
        """A categorized invoice line with no account_code flags blank_account_code."""
        inv = self._clean_invoice_with_account(
            lines=[InvoiceLine(description="Goods", net_amount=100.0, gst_amount=9.0,
                                account_code="")],
        )
        tripped, reasons = detect_struggle(_state([inv]))
        assert tripped is True
        assert any(r.startswith("blank_account_code") for r in reasons)

    def test_blank_account_code_does_not_fire_for_other_doc_type(self):
        """'other' / 'expense_claim' lines legitimately may not have an
        account_code; the flag would be a false positive on those lanes."""
        inv = self._clean_invoice_with_account(
            lines=[InvoiceLine(description="Petty cash", net_amount=50.0, gst_amount=0.0,
                                account_code="")],
        )
        tripped, reasons = detect_struggle(
            _state([inv], doc_type="expense_claim"),
        )
        assert not any(r.startswith("blank_account_code") for r in reasons), (
            f"blank_account_code should NOT fire for expense_claim. Got: {reasons}"
        )

    def test_account_code_not_in_coa_flags(self):
        """An account_code that survives categorization but isn't in the
        client COA must flag — the LLM step is supposed to null these out
        (categorizer.py:265-266) but a wrong-client entity-memory entry
        can bypass that."""
        inv = self._clean_invoice_with_account(
            lines=[InvoiceLine(description="Goods", net_amount=100.0, gst_amount=9.0,
                                account_code="XXX-INVALID-CODE")],
        )
        state = _state([inv])
        # Inject a COA that does NOT include the line's account_code.
        # coa_from_state reads from the "coa" key (list of dicts with
        # 'code' / 'description' fields).
        state["coa"] = [
            {"code": "6100", "description": "Office Expenses"},
            {"code": "6200", "description": "Travel"},
        ]
        tripped, reasons = detect_struggle(state)
        assert tripped is True
        assert any(r.startswith("account_code_not_in_coa") for r in reasons)
        # The reported code must be the offending one.
        flagged = [r for r in reasons if r.startswith("account_code_not_in_coa")]
        assert "XXX-INVALID-CODE" in flagged[0]

    def test_currency_mismatch_flags_my_doc_with_sgd(self):
        """An MY-jurisdiction doc whose currency defaulted to SGD must flag —
        this is the D6 silent-corruption guard."""
        inv = self._clean_invoice_with_account(currency="SGD")
        state = _state([inv])
        state[nodes.TAX_JURISDICTION_KEY] = "MALAYSIA"
        tripped, reasons = detect_struggle(state)
        assert tripped is True
        assert any(r.startswith("currency_mismatch") for r in reasons)

    def test_no_flags_when_all_clean(self):
        """A clean, categorized, MY-jurisdiction MYR invoice does NOT trip."""
        inv = self._clean_invoice_with_account(currency="MYR")
        state = _state([inv])
        state[nodes.TAX_JURISDICTION_KEY] = "MALAYSIA"
        tripped, reasons = detect_struggle(state)
        assert tripped is False, f"Clean MY doc tripped: {reasons}"
