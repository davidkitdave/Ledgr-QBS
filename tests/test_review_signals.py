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
        lines=[InvoiceLine(description="Goods", net_amount=100.0, gst_amount=9.0)],
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
        lines=[InvoiceLine(description="Service", net_amount=100.0, gst_amount=None)],
    )
    tripped, reasons = detect_struggle(_state([inv]))
    assert tripped is False
    assert reasons == []
