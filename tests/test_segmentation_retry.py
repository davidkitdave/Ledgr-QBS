"""Hermetic tests for G2-failure segmentation retry on the understand path."""

from __future__ import annotations

import pytest

from invoice_processing.extract.ledger_extract import (
    ExtractedDocument,
    ExtractedDocumentBundle,
    ExtractedDocumentLine,
)
from invoice_processing.extract.process_invoice_document import process_invoice_document

pytestmark = pytest.mark.unit


def _doc(
    reference: str,
    *,
    grand_total: float,
    page_range: list[int] | None = None,
) -> ExtractedDocument:
    return ExtractedDocument(
        doc_type="invoice",
        page_range=page_range or [1, 1],
        vendor="Vendor-A",
        buyer="Buyer-B",
        reference=reference,
        date="2025-12-01",
        currency="MYR",
        presentation="summary",
        lines=[
            ExtractedDocumentLine(
                description="Parts",
                net_amount=grand_total,
                gst_amount=0.0,
            ),
        ],
        subtotal=grand_total,
        tax_total=0.0,
        grand_total=grand_total,
        tax_lines=[],
        direction_for_client="purchase",
        tax_visible_on_document=False,
    )


def _bad_gap_bundle() -> ExtractedDocumentBundle:
    return ExtractedDocumentBundle(
        documents=[
            _doc("INV-A", grand_total=200.0, page_range=[1, 1]),
            _doc("INV-C", grand_total=60.0, page_range=[3, 3]),
        ],
        skipped_pages=None,
    )


def _good_full_coverage_bundle() -> ExtractedDocumentBundle:
    return ExtractedDocumentBundle(
        documents=[
            _doc("INV-A", grand_total=200.0, page_range=[1, 1]),
            _doc("INV-B", grand_total=60.0, page_range=[2, 2]),
            _doc("INV-C", grand_total=40.0, page_range=[3, 3]),
        ],
        skipped_pages=None,
    )


def _patch_understand_path(monkeypatch, fake_extract):
    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.EXTRACT_LEDGER_FN",
        fake_extract,
    )
    monkeypatch.setattr(
        "invoice_processing.extract.process_invoice_document.count_input_pages",
        lambda _data, _mime: 3,
    )
    monkeypatch.delenv("LEDGR_CAPTURE_BOOK", raising=False)
    monkeypatch.setenv("LEDGR_UNDERSTAND_EXTRACT", "1")


def test_segmentation_retry_second_pass_fixes_g2(monkeypatch):
    """First bundle fails G2, second passes — no segmentation flag, two extracts."""
    calls: list[str | None] = []

    def fake_extract(data, mime_type, **kwargs):
        calls.append(kwargs.get("hint"))
        if len(calls) == 1:
            return _bad_gap_bundle()
        return _good_full_coverage_bundle()

    _patch_understand_path(monkeypatch, fake_extract)

    result = process_invoice_document(
        b"%PDF",
        "application/pdf",
        doc_type="invoice",
        direction="purchase",
        our_gst_registered=True,
        base_currency="MYR",
    )

    assert len(calls) == 2
    assert calls[1] is not None
    assert "Re-segment:" in calls[1]
    assert len(result.normalized) == 3
    assert all(inv.reconciled for inv in result.normalized)
    assert not any(
        "segmentation uncertain" in (inv.reconcile_note or "").lower()
        for inv in result.normalized
    )
    assert not any("gaps" in w for w in result.partial_failure_warnings)


def test_segmentation_retry_both_passes_fail_g2(monkeypatch):
    """Both bundles fail G2 — still flagged, extract called at most twice."""
    calls: list[str | None] = []

    def fake_extract(data, mime_type, **kwargs):
        calls.append(kwargs.get("hint"))
        return _bad_gap_bundle()

    _patch_understand_path(monkeypatch, fake_extract)

    result = process_invoice_document(
        b"%PDF",
        "application/pdf",
        doc_type="invoice",
        direction="purchase",
        our_gst_registered=True,
        base_currency="MYR",
    )

    assert len(calls) == 2
    assert len(result.normalized) == 2
    for inv in result.normalized:
        note = (inv.reconcile_note or "").lower()
        assert "segmentation uncertain" in note
        assert "gaps" in note
    assert any("gaps" in w for w in result.partial_failure_warnings)


def test_segmentation_retry_skipped_when_first_pass_g2_ok(monkeypatch):
    """First pass G2 ok — extract called once, no retry."""
    calls: list[str | None] = []

    def fake_extract(data, mime_type, **kwargs):
        calls.append(kwargs.get("hint"))
        return _good_full_coverage_bundle()

    _patch_understand_path(monkeypatch, fake_extract)

    result = process_invoice_document(
        b"%PDF",
        "application/pdf",
        doc_type="invoice",
        direction="purchase",
        our_gst_registered=True,
        base_currency="MYR",
    )

    assert len(calls) == 1
    assert len(result.normalized) == 3
    assert all(inv.reconciled for inv in result.normalized)
    assert not any(
        "segmentation uncertain" in (inv.reconcile_note or "").lower()
        for inv in result.normalized
    )
