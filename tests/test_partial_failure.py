"""Tests for G5 partial-failure semantics (WS-2.5)."""

from __future__ import annotations

from datetime import date

from ledgr_slack.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from ledgr_slack.delivery_notes import (
    build_partial_failure_warnings,
    format_partial_failure_note,
)


def _inv(*, number: str, reconciled: bool) -> NormalizedInvoice:
    return NormalizedInvoice(
        doc_type="purchase",
        invoice_number=number,
        invoice_date=date(2025, 12, 1),
        currency="MYR",
        supplier=PartyInfo(name="Vendor"),
        reconciled=reconciled,
        lines=[InvoiceLine(description="x", net_amount=10.0, gst_amount=0.0)],
    )


def test_mixed_reconcile_produces_partial_warning():
    warnings = build_partial_failure_warnings(
        [_inv(number="INV-OK", reconciled=True), _inv(number="INV-BAD", reconciled=False)],
        page_coverage_ok=True,
        page_coverage_detail="ok",
        input_page_count=2,
    )
    assert len(warnings) == 1
    assert "1 of 2" in warnings[0]
    assert "INV-BAD" in warnings[0]


def test_page_gap_warning_without_dropping_good_docs():
    warnings = build_partial_failure_warnings(
        [_inv(number="A", reconciled=True), _inv(number="C", reconciled=True)],
        page_coverage_ok=False,
        page_coverage_detail="gaps on pages [2]",
        input_page_count=3,
    )
    assert any("gaps" in w for w in warnings)
    assert any("may be missing" in w for w in warnings)


def test_format_partial_failure_note():
    note = format_partial_failure_note(["partial extraction: 1 of 2 failed"])
    assert note.startswith("⚠️")
    assert "partial extraction" in note
