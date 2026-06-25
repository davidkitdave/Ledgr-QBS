"""Hermetic tests for G2 page-coverage gates (WS-2.3)."""

from __future__ import annotations

import pytest

from invoice_processing.extract.ledger_extract import ExtractedDocumentBundle
from invoice_processing.extract.segmentation_gates import (
    count_input_pages,
    validate_bundle_page_coverage,
    validate_page_ranges,
)

pytestmark = pytest.mark.unit


class TestValidatePageRanges:
    def test_full_coverage_with_skipped_cover(self):
        ok, detail = validate_page_ranges(
            [(2, 3), (4, 5)],
            total_pages=5,
            skipped_pages=[1],
        )
        assert ok, detail

    def test_detects_gap(self):
        ok, detail = validate_page_ranges([(1, 2), (4, 4)], total_pages=4)
        assert not ok
        assert "gaps" in detail

    def test_detects_overlap(self):
        ok, detail = validate_page_ranges([(1, 2), (2, 3)], total_pages=3)
        assert not ok
        assert "more than one" in detail

    def test_detects_dropped_trailing_page(self):
        ok, detail = validate_page_ranges([(1, 2)], total_pages=3)
        assert not ok
        assert "gaps" in detail


class TestValidateBundlePageCoverage:
    def test_valid_bundle_passes(self):
        from tests.test_process_invoice_understand_fanout import _doc

        bundle = ExtractedDocumentBundle(
            documents=[
                _doc("A", grand_total=10.0, page_range=[1, 1]),
                _doc("B", grand_total=20.0, page_range=[2, 2]),
            ],
            skipped_pages=None,
        )
        ok, detail = validate_bundle_page_coverage(bundle, total_pages=2)
        assert ok, detail

    def test_gap_fails(self):
        from tests.test_process_invoice_understand_fanout import _doc

        bundle = ExtractedDocumentBundle(
            documents=[
                _doc("A", grand_total=10.0, page_range=[1, 1]),
                _doc("B", grand_total=20.0, page_range=[3, 3]),
            ],
            skipped_pages=None,
        )
        ok, detail = validate_bundle_page_coverage(bundle, total_pages=3)
        assert not ok
        assert "gaps" in detail


class TestCountInputPages:
    def test_pdf_page_count_from_minimal_pdf(self):
        from tests.test_bank_bytes import _make_digital_pdf_bytes

        pdf = _make_digital_pdf_bytes()
        assert count_input_pages(pdf, "application/pdf") == 1

    def test_image_is_single_page(self):
        assert count_input_pages(b"\xff\xd8\xff", "image/jpeg") == 1
