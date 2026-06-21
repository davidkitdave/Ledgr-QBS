"""Telco bill Phase 2 summarization — SR/ZR from GST buckets, not per-line detail."""

from __future__ import annotations

from invoice_processing.export.exporters import _load_erp_profile
from invoice_processing.export.exporters import XeroLedgerExporter
from invoice_processing.export.tax_classifier import TaxClassifier
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
    def test_collapses_detail_to_sr_zr_lines(self):
        inv = normalize_document_record(
            _telco_bill_a_capture(),
            direction="purchase",
            our_gst_registered=True,
            mapper_version="enhanced",
            erp_profile=_AUTOCOUNT_PROFILE,
        )
        assert len(inv.lines) == 2
        assert inv.lines[0].net_amount == 1164.42
        assert inv.lines[0].gst_amount == 104.80
        assert inv.lines[1].net_amount == 58.93
        assert inv.doc_total == 1328.15
        assert inv.reconciled is True

    def test_xero_export_sr_and_zr(self):
        inv = normalize_document_record(
            _telco_bill_a_capture(),
            direction="purchase",
            our_gst_registered=True,
            mapper_version="enhanced",
            erp_profile=_AUTOCOUNT_PROFILE,
        )
        tax = TaxClassifier()
        for line in inv.lines:
            tax.classify_line(line, inv)
        rows = XeroLedgerExporter(tax).rows([inv], "purchase")
        assert [r["*TaxType"] for r in rows] == ["SR", "ZR"]
        assert [r["TaxAmount"] for r in rows] == [104.80, 0.0]
        assert float(rows[0]["*UnitAmount"]) == 1164.42
        assert float(rows[1]["*UnitAmount"]) == 58.93

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
        assert len(invoices[0].lines) == 2


def test_slim_document_record_for_state_strips_telco_line_items():
    from invoice_processing.extract.document_normalizer import slim_document_record_for_state

    record = _telco_bill_a_capture()
    assert len(record.line_items) == 300
    slim = slim_document_record_for_state(record)
    assert slim["line_items"] == []
    assert slim["tables"] == []
    assert any("GST @ 9%" in f["label"] for f in slim["labeled_fields"])


def test_telco_dedupes_duplicate_gst_buckets():
    from invoice_processing.export.line_grouping import telco_gst_bucket_lines

    record = _telco_bill_a_capture()
    dup_fields = list(record.labeled_fields) + [
        LabeledField(label="GST @ 9% on $1,164.42", value="$104.80"),
        LabeledField(label="GST @ 0% on $58.93", value="$0.00"),
    ]
    record.labeled_fields = dup_fields
    lines = telco_gst_bucket_lines(record)
    assert lines is not None
    assert len(lines) == 2
