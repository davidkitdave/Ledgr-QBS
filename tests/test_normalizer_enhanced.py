"""Enhanced normalizer mapper tests (V1)."""

from invoice_processing.extract.document_normalizer import normalize_document_record
from invoice_processing.extract.document_record import (
    DocumentRecord,
    LabeledField,
    LineCapture,
)


def test_enhanced_finds_invoice_hash_label():
    rec = DocumentRecord(
        labeled_fields=[LabeledField(label="Invoice #", value="MGT-2025-011-INV")],
        line_items=[LineCapture(description="Fee", net_amount=6500.0)],
        totals=[LabeledField(label="Total Amount", value="6500.00")],
    )
    inv = normalize_document_record(rec, direction="purchase", mapper_version="enhanced")
    assert inv.invoice_number == "MGT-2025-011-INV"


def test_enhanced_currency_dollar_sign():
    rec = DocumentRecord(
        labeled_fields=[LabeledField(label="Total", value="$ 800.00")],
        line_items=[LineCapture(description="Line", net_amount=800.0, currency="$")],
        totals=[LabeledField(label="Total", value="USD 800.00")],
    )
    inv = normalize_document_record(rec, direction="purchase", mapper_version="enhanced")
    assert inv.currency == "USD"
