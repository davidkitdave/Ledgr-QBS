"""Telco bill Phase 2 — verbatim capture lines (WS-4.1)."""

from __future__ import annotations

from invoice_processing.export.exporters import _load_erp_profile
from invoice_processing.extract.document_normalizer import normalize_document_record
from invoice_processing.extract.document_record import (
    DocumentRecord,
    DocumentRecordBundle,
    LabeledField,
    LineCapture,
)

_AUTOCOUNT_PROFILE = _load_erp_profile("autocount.yaml")


def _telco_bill_a_capture() -> DocumentRecord:
    """Hermetic capture shaped like BV-0002830 Telco Provider A bill (GST summary fields)."""
    detail_lines = [
        LineCapture(description=f"Mobile line detail {i}", net_amount=52.0)
        for i in range(300)
    ]
    return DocumentRecord(
        notes="Telco Provider A Ltd business bill",
        labeled_fields=[
            LabeledField(label="Tax Invoice GST Reg. No", value="M9-0005650-C"),
            LabeledField(label="Bill No.", value="8004483920122025"),
            LabeledField(label="Date of Bill", value="04/12/25"),
            LabeledField(label="Due Date", value="18/12/25"),
            LabeledField(label="GST @ 9% on $1,164.42", value="$104.80"),
            LabeledField(label="GST @ 8% on $0.00", value="$0.00"),
            LabeledField(label="GST @ 7% on $0.00", value="$0.00"),
            LabeledField(label="GST @ 0% on $58.93", value="$0.00"),
        ],
        line_items=detail_lines,
        totals=[
            LabeledField(label="CURRENT CHARGES", value="$1,328.15"),
            LabeledField(label="Total GST", value="$104.80"),
        ],
        parties=[],
    )


class TestTelcoSummary:
    def test_keeps_all_capture_lines_verbatim(self):
        inv = normalize_document_record(
            _telco_bill_a_capture(),
            direction="purchase",
            our_gst_registered=True,
            mapper_version="enhanced",
        )
        assert len(inv.lines) == 300
        assert inv.lines[0].description == "Mobile line detail 0"
        assert inv.lines[0].net_amount == 52.0

    def test_erp_profile_does_not_collapse_telco_lines(self):
        inv = normalize_document_record(
            _telco_bill_a_capture(),
            direction="purchase",
            our_gst_registered=True,
            mapper_version="enhanced",
            erp_profile=_AUTOCOUNT_PROFILE,
        )
        assert len(inv.lines) == 300
        assert inv.lines[0].net_amount == 52.0

    def test_not_expense_reimbursement(self):
        inv = normalize_document_record(
            _telco_bill_a_capture(),
            direction="purchase",
            our_gst_registered=True,
            mapper_version="enhanced",
            erp_profile=_AUTOCOUNT_PROFILE,
        )
        assert inv.lines[0].description != "Expense reimbursement"

    def test_bundle_with_many_lines_still_one_invoice(self):
        bundle = DocumentRecordBundle(documents=[_telco_bill_a_capture()])
        from invoice_processing.extract.document_normalizer import normalize_document_bundle

        invoices = normalize_document_bundle(
            bundle,
            direction="purchase",
            our_gst_registered=True,
            erp_profile=_AUTOCOUNT_PROFILE,
        )
        assert len(invoices) == 1
        assert len(invoices[0].lines) == 300


def test_slim_document_record_for_state_strips_telco_line_items():
    from invoice_processing.extract.document_normalizer import slim_document_record_for_state

    record = _telco_bill_a_capture()
    assert len(record.line_items) == 300
    slim = slim_document_record_for_state(record)
    assert slim["line_items"] == []
    assert slim["tables"] == []
    assert any("GST @ 9%" in f["label"] for f in slim["labeled_fields"])
