"""Hermetic tests for deterministic SOA cover-page drop (no LLM).

Covers ``_drop_soa_cover_documents`` in ``invoice_processing.extract.ledger_extract``:
the post-processing gate that removes a Statement-of-Account / summary cover the
model sometimes returns alongside the real invoices it itemizes (double-counting
the totals).
"""

from __future__ import annotations

from invoice_processing.extract.ledger_extract import (
    ExtractedDocument,
    ExtractedDocumentBundle,
    _drop_soa_cover_documents,
)
from invoice_processing.extract.segmentation_gates import (
    validate_bundle_page_coverage,
)


def _doc(
    *,
    doc_type: str,
    page: int,
    grand_total: float,
    reference: str = "REF",
) -> ExtractedDocument:
    """Build a minimal single-page ExtractedDocument for these gate tests."""
    return ExtractedDocument(
        doc_type=doc_type,
        page_range=[page, page],
        vendor="Vendor-A",
        reference=reference,
        date="2026-01-01",
        currency="MYR",
        grand_total=grand_total,
    )


def test_auto_lab_shaped_statement_cover_dropped():
    """1 statement (p1, 5783) + 6 invoices (p2-7 summing to 5783) -> 6 invoices."""
    invoice_totals = [280.0, 705.0, 168.0, 735.0, 2545.0, 1350.0]
    assert sum(invoice_totals) == 5783.0
    docs = [_doc(doc_type="statement", page=1, grand_total=5783.0, reference="DJ0161C")]
    for i, total in enumerate(invoice_totals):
        docs.append(_doc(doc_type="invoice", page=i + 2, grand_total=total, reference=f"INV{i}"))
    bundle = ExtractedDocumentBundle(documents=docs)

    result = _drop_soa_cover_documents(bundle)

    assert len(result.documents) == 6
    assert all(d.doc_type == "invoice" for d in result.documents)
    assert all(d.grand_total != 5783.0 for d in result.documents)
    assert result.skipped_pages == [1]


def test_single_statement_only_unchanged():
    """A lone statement is never a redundant cover -> not dropped."""
    bundle = ExtractedDocumentBundle(
        documents=[_doc(doc_type="statement", page=1, grand_total=500.0)]
    )
    result = _drop_soa_cover_documents(bundle)
    assert len(result.documents) == 1
    assert result.documents[0].doc_type == "statement"
    assert result.skipped_pages is None


def test_single_invoice_only_unchanged():
    """A lone invoice is left intact."""
    bundle = ExtractedDocumentBundle(
        documents=[_doc(doc_type="invoice", page=1, grand_total=500.0)]
    )
    result = _drop_soa_cover_documents(bundle)
    assert len(result.documents) == 1
    assert result.documents[0].doc_type == "invoice"
    assert result.skipped_pages is None


def test_arithmetic_fallback_drops_mislabeled_cover():
    """No 'statement' doc, but one 'invoice' total == sum of others -> dropped."""
    docs = [
        _doc(doc_type="invoice", page=1, grand_total=900.0, reference="COVER"),
        _doc(doc_type="invoice", page=2, grand_total=300.0, reference="A"),
        _doc(doc_type="invoice", page=3, grand_total=600.0, reference="B"),
    ]
    bundle = ExtractedDocumentBundle(documents=docs)

    result = _drop_soa_cover_documents(bundle)

    assert len(result.documents) == 2
    assert {d.reference for d in result.documents} == {"A", "B"}
    assert result.skipped_pages == [1]


def test_guard_all_statements_drops_nothing():
    """Every doc is a statement -> dropping would empty the bundle -> drop nothing."""
    docs = [
        _doc(doc_type="statement", page=1, grand_total=100.0),
        _doc(doc_type="statement", page=2, grand_total=200.0),
    ]
    bundle = ExtractedDocumentBundle(documents=docs)

    result = _drop_soa_cover_documents(bundle)

    assert len(result.documents) == 2
    assert result.skipped_pages is None


def test_two_invoices_no_match_drops_nothing():
    """Two invoices, no statement, no total==sum-of-others -> nothing dropped."""
    docs = [
        _doc(doc_type="invoice", page=1, grand_total=300.0),
        _doc(doc_type="invoice", page=2, grand_total=600.0),
    ]
    bundle = ExtractedDocumentBundle(documents=docs)

    result = _drop_soa_cover_documents(bundle)

    assert len(result.documents) == 2
    assert not result.skipped_pages  # None or empty


def test_page_coverage_valid_after_drop():
    """After dropping the cover, remaining pages + skipped_pages cover all 7 pages."""
    invoice_totals = [280.0, 705.0, 168.0, 735.0, 2545.0, 1350.0]
    docs = [_doc(doc_type="statement", page=1, grand_total=5783.0, reference="DJ0161C")]
    for i, total in enumerate(invoice_totals):
        docs.append(_doc(doc_type="invoice", page=i + 2, grand_total=total, reference=f"INV{i}"))
    bundle = ExtractedDocumentBundle(documents=docs)

    result = _drop_soa_cover_documents(bundle)

    ok, detail = validate_bundle_page_coverage(result, total_pages=7)
    assert ok, detail
