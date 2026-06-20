"""Hermetic SOA gate tests for the two-phase DocumentRecord path."""

from __future__ import annotations

import pytest

from invoice_processing.extract.document_normalizer import normalize_document_bundle
from invoice_processing.extract.document_record import (
    DocumentRecord,
    DocumentRecordBundle,
    LabeledField,
    LineCapture,
)


def _soa_cover_table() -> DocumentRecord:
    """SOA page-1 summary table — invoice refs as line descriptions, no doc invoice #."""
    return DocumentRecord(
        doc_kind_guess="statement of account",
        labeled_fields=[LabeledField(label="DEBTOR STATEMENT", value="Sample Vendor Inc")],
        line_items=[
            LineCapture(description="IA-07316", net_amount=100.0),
            LineCapture(description="IA-07330", net_amount=200.0),
            LineCapture(description="IA-07332", net_amount=150.0),
            LineCapture(description="IA-07365", net_amount=80.0),
            LineCapture(description="IA-07368", net_amount=90.0),
            LineCapture(description="IA-07383", net_amount=110.0),
            LineCapture(description="IA-07392", net_amount=120.0),
            LineCapture(description="IA-07428", net_amount=130.0),
        ],
    )


def _embedded_invoice(number: str, *, lines: list[tuple[str, float]]) -> DocumentRecord:
    return DocumentRecord(
        doc_kind_guess="invoice",
        labeled_fields=[
            LabeledField(label="Invoice Number", value=number),
            LabeledField(label="Currency", value="MYR"),
        ],
        line_items=[
            LineCapture(description=desc, net_amount=amt) for desc, amt in lines
        ],
        totals=[LabeledField(label="Total", value=str(sum(a for _, a in lines)))],
    )


EXPECTED_NUMBERS = {
    "CNA-00176", "IA-07465", "IA-07467", "IA-07514", "IA-07522",
    "IA-07526", "IA-07527", "IA-07573", "IA-07588", "IA-07590",
}
PHANTOM_NUMBERS = {
    "IA-07316", "IA-07330", "IA-07332", "IA-07365", "IA-07368",
    "IA-07383", "IA-07392", "IA-07428",
}


class TestSoaDocumentRecordGate:
    def test_soa_cover_table_dropped(self):
        bundle = DocumentRecordBundle(
            documents=[_soa_cover_table()],
            skipped_pages=[1],
        )
        out = normalize_document_bundle(bundle, direction="purchase", base_currency="MYR")
        assert out == []

    def test_soa_cover_plus_real_invoices(self):
        bundle = DocumentRecordBundle(
            documents=[
                _soa_cover_table(),
                _embedded_invoice("IA-07465", lines=[("Consulting services", 385.0)]),
                _embedded_invoice("IA-07467", lines=[
                    ("Parts supply", 875.0),
                    ("Labour charge", 900.0),
                ]),
            ],
            skipped_pages=[1],
        )
        out = normalize_document_bundle(bundle, direction="purchase", base_currency="MYR")
        assert len(out) == 2
        nums = {inv.invoice_number for inv in out}
        assert nums == {"IA-07465", "IA-07467"}
        assert sum(len(inv.lines) for inv in out) == 3

    def test_phantom_invoice_sentinel_lines_dropped(self):
        bundle = DocumentRecordBundle(
            documents=[
                DocumentRecord(
                    line_items=[LineCapture(description="INVOICE", net_amount=100.0)],
                ),
                _embedded_invoice("CNA-00176", lines=[("Credit adjustment", -100.0)]),
            ],
        )
        out = normalize_document_bundle(bundle, direction="purchase", base_currency="MYR")
        assert len(out) == 1
        assert out[0].invoice_number == "CNA-00176"


class TestClientNotReimbursement:
    def test_aaa_invoice_number_not_treated_as_expense_claim(self):
        rec = DocumentRecord(
            doc_kind_guess="invoice",
            labeled_fields=[
                LabeledField(label="Invoice Number", value="MGT-2025-011-INV"),
                LabeledField(label="Invoice Date", value="15 Jan 2025"),
                LabeledField(label="Currency", value="USD"),
            ],
            line_items=[
                LineCapture(
                    description="Consultation Management Fee for Aviation Audit",
                    net_amount=6500.0,
                    currency="USD",
                ),
            ],
            totals=[LabeledField(label="Total", value="6500.00")],
        )
        from invoice_processing.extract.document_normalizer import normalize_document_record

        inv = normalize_document_record(rec, direction="purchase", base_currency="SGD")
        assert inv.invoice_number == "MGT-2025-011-INV"
        assert inv.lines[0].description.startswith("Consultation")
        assert inv.doc_total == pytest.approx(6500.0)


# ── Live gate (optional) ─────────────────────────────────────────────────────

_REL_PATH = (
    "MYDoc/Sample Auto Enterprise/Purchase/SOA-SAMPLE-DEC-2025_.pdf"
)
_TEST_DOC_DIR = __import__("os").environ.get("LEDGR_TEST_DOC_DIR")
_PDF_PATH = (
    __import__("pathlib").Path(_TEST_DOC_DIR).expanduser() / _REL_PATH
    if _TEST_DOC_DIR
    else None
)
_needs_pdf = pytest.mark.skipif(
    _PDF_PATH is None or not (_PDF_PATH and _PDF_PATH.exists()),
    reason="Set LEDGR_TEST_DOC_DIR to LocalTest/TestDoc for live SOA test",
)


@_needs_pdf
def test_cool_power_two_phase_live():
    """Live two-phase path: 10 invoices, 22 lines, no SOA phantoms."""
    from invoice_processing.extract.document_extractor import extract_document_file
    from invoice_processing.extract.record_merge import merge_document_records

    bundle = extract_document_file(_PDF_PATH)
    bundle = merge_document_records(bundle)
    out = normalize_document_bundle(bundle, direction="purchase", base_currency="MYR")

    numbers = {inv.invoice_number for inv in out if inv.invoice_number}
    skipped = bundle.skipped_pages or []
    # Phase 1 may record skipped_pages OR leave cover as doc 1 (dropped in Phase 2).
    assert 1 in skipped or len(bundle.documents) >= 10, (
        f"expected SOA cover skipped or split; skipped_pages={skipped}, docs={len(bundle.documents)}"
    )
    assert len(out) == 10, f"expected 10 invoices, got {len(out)}: {sorted(numbers)}"
    assert sum(len(inv.lines) for inv in out) == 22
    assert numbers & PHANTOM_NUMBERS == set(), f"phantoms survived: {numbers & PHANTOM_NUMBERS}"
    assert EXPECTED_NUMBERS <= numbers, f"missing: {EXPECTED_NUMBERS - numbers}"
