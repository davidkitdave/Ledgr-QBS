"""Tests for expense package merge heuristics."""

from invoice_processing.extract.document_record import (
    DocumentRecord,
    DocumentRecordBundle,
    LabeledField,
    LineCapture,
    PartyCapture,
)
from invoice_processing.extract.record_merge import merge_document_records, should_merge_package


def test_should_merge_expense_package():
    claim = DocumentRecord(
        parties=[PartyCapture(name="Supplier Gamma", role_hint="employee")],
        labeled_fields=[LabeledField(label="Claim", value="AAI-25-040")],
        line_items=[LineCapture(description="Travel", net_amount=100.0)],
    )
    receipt = DocumentRecord(line_items=[LineCapture(description="Taxi", net_amount=10.0)])
    assert should_merge_package([claim, receipt])


def test_should_not_merge_distinct_invoices():
    a = DocumentRecord(
        labeled_fields=[LabeledField(label="Invoice Number", value="INV-2026-003")],
        totals=[LabeledField(label="Total", value="100")],
    )
    b = DocumentRecord(
        labeled_fields=[LabeledField(label="Invoice Number", value="INV-2025-012")],
        totals=[LabeledField(label="Total", value="200")],
    )
    assert not should_merge_package([a, b])


def test_merge_collapses_to_one():
    claim = DocumentRecord(
        parties=[PartyCapture(name="Supplier Gamma", role_hint="employee")],
        line_items=[LineCapture(description="Travel", net_amount=100.0)],
    )
    receipt = DocumentRecord(line_items=[LineCapture(description="Taxi", net_amount=10.0)])
    bundle = merge_document_records(DocumentRecordBundle(documents=[claim, receipt]))
    assert len(bundle.documents) == 1
    assert len(bundle.documents[0].line_items) == 2
