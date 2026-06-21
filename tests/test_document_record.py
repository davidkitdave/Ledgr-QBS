"""Tests for DocumentRecord schema and Phase 1/2 modules (hermetic — no network)."""

from __future__ import annotations

import pytest

from invoice_processing.extract.document_extractor import PHASE1_PROMPT
from invoice_processing.extract.document_normalizer import normalize_document_record
from invoice_processing.extract.document_record import (
    AnnotationCapture,
    DocumentRecord,
    DocumentRecordBundle,
    LabeledField,
    LineCapture,
    PartyCapture,
)
from invoice_processing.extract.invoice_extractor import extract_invoice_bundle


class TestDocumentRecordSchema:
    def test_empty_record_validates(self):
        rec = DocumentRecord()
        assert rec.labeled_fields == []
        assert rec.line_items == []

    def test_bundle_roundtrip(self):
        rec = DocumentRecord(
            labeled_fields=[LabeledField(label="Invoice Number", value="INV-1")],
            line_items=[LineCapture(description="Service", net_amount=100.0)],
        )
        bundle = DocumentRecordBundle(documents=[rec])
        data = bundle.model_dump()
        restored = DocumentRecordBundle.model_validate(data)
        assert restored.documents[0].labeled_fields[0].value == "INV-1"


class TestPhase1Prompt:
    def test_prompt_does_not_summarize_for_bookkeeping(self):
        assert "NOT" in PHASE1_PROMPT or "not" in PHASE1_PROMPT.lower()
        assert "ledger summary" in PHASE1_PROMPT.lower() or "summarize" in PHASE1_PROMPT.lower()

    def test_hint_appended_to_prompt(self):
        from invoice_processing.extract.invoice_extractor import _append_hint as inv_hint

        out = inv_hint("base", "treat as reimbursement")
        assert "reimbursement" in out


class TestHintParam:
    def test_extract_invoice_bundle_accepts_hint_kwarg(self):
        """P0-1: hint param exists — integration test uses mock client separately."""
        import inspect

        sig = inspect.signature(extract_invoice_bundle)
        assert "hint" in sig.parameters


class TestNormalizeDocumentRecord:
    def _sample_d32(self) -> DocumentRecord:
        return DocumentRecord(
            labeled_fields=[
                LabeledField(label="Invoice Number", value="INV-2026-003"),
                LabeledField(label="Invoice Date", value="2025-12-01"),
                LabeledField(label="Currency", value="USD"),
            ],
            parties=[
                PartyCapture(name="Vendor Alpha Pte Ltd", role_hint="sender_block"),
                PartyCapture(name="Acme Client - AC", role_hint="to_block"),
            ],
            line_items=[
                LineCapture(description="PTTEP/UOA monitoring audit", quantity=1, unit_amount=500.0, net_amount=500.0),
                LineCapture(description="Create Report", quantity=1, unit_amount=200.0, net_amount=200.0),
            ],
            totals=[
                LabeledField(label="Sub Total", value="700.00"),
                LabeledField(label="Total", value="700.00"),
            ],
            annotations=[AnnotationCapture(text="Paid 14 Jan 26 AAI Wise PG", kind="payment_stamp")],
        )

    def test_d32_maps_to_purchase_lines(self):
        inv = normalize_document_record(self._sample_d32(), direction="purchase", base_currency="SGD")
        assert inv.invoice_number == "INV-2026-003"
        assert len(inv.lines) == 2
        assert inv.supplier.name == "Vendor Alpha Pte Ltd"
        assert inv.needs_fx_review is False  # USD invoice booked in USD
        assert inv.currency == "USD"

    def test_text_invoice_date_parsed(self):
        rec = DocumentRecord(
            labeled_fields=[
                LabeledField(label="Invoice Number", value="MGT-2025-011"),
                LabeledField(label="Invoice Date", value="15 Jan 2025"),
                LabeledField(label="Currency", value="USD"),
            ],
            line_items=[LineCapture(description="Services", net_amount=6500.0)],
            totals=[LabeledField(label="Total", value="6500.00")],
        )
        inv = normalize_document_record(rec, direction="purchase", base_currency="SGD")
        assert str(inv.invoice_date) == "2025-01-15"

    def test_date_range_parsed(self):
        rec = DocumentRecord(
            labeled_fields=[
                LabeledField(label="Invoice Number", value="INV-2026-003"),
                LabeledField(label="Date", value="17th November 2025 - 19th November 2025"),
                LabeledField(label="Currency", value="USD"),
            ],
            line_items=[LineCapture(description="Work", net_amount=700.0)],
            totals=[LabeledField(label="Total", value="700.00")],
        )
        inv = normalize_document_record(rec, direction="purchase", base_currency="SGD")
        assert str(inv.invoice_date) == "2025-11-19"

    def test_mixed_currency_expense_claim_keeps_verbatim_lines(self):
        rec = DocumentRecord(
            doc_kind_guess="expense claim",
            labeled_fields=[
                LabeledField(label="Task Ref", value="AAI-25-040"),
                LabeledField(label="Currency", value="USD"),
            ],
            parties=[PartyCapture(name="Supplier Gamma", role_hint="employee")],
            line_items=[
                LineCapture(description="Receipt A", net_amount=100000.0, currency="IDR"),
                LineCapture(description="Receipt B", net_amount=200000.0, currency="IDR"),
                LineCapture(description="Reimbursement total", net_amount=311.79, currency="USD"),
            ],
            totals=[LabeledField(label="Total", value="311.79 USD")],
        )
        inv = normalize_document_record(rec, direction="purchase", base_currency="SGD")
        assert inv.invoice_number == "AAI-25-040"
        assert inv.supplier.name == "Supplier Gamma"
        assert inv.currency == "USD"
        assert len(inv.lines) == 3
        assert inv.lines[0].description == "Receipt A"
        assert inv.lines[2].net_amount == pytest.approx(311.79)

    def test_unresolved_mixed_currency_still_flagged(self):
        rec = DocumentRecord(
            labeled_fields=[LabeledField(label="Currency", value="USD")],
            line_items=[
                LineCapture(description="Item A", net_amount=100.0, currency="IDR"),
                LineCapture(description="Item B", net_amount=50.0, currency="USD"),
            ],
            totals=[LabeledField(label="Total", value="999.00")],
        )
        inv = normalize_document_record(rec, direction="purchase", base_currency="SGD")
        assert inv.needs_fx_review is True

    def test_ambiguous_direction_unknown_flagged(self):
        rec = DocumentRecord(
            labeled_fields=[LabeledField(label="Invoice Number", value="X-1")],
            line_items=[LineCapture(description="Item", net_amount=10.0)],
            totals=[LabeledField(label="Total", value="10.00")],
        )
        inv = normalize_document_record(rec, direction="unknown", base_currency="SGD")
        assert inv.reconciled is False
        assert "unknown" in (inv.reconcile_note or "").lower()


class TestAntiHardcode:
    def test_no_expense_claim_doc_type_branch_in_extractor(self):
        import invoice_processing.extract.document_extractor as mod

        src = open(mod.__file__).read()
        assert "expense_claim" not in src
        assert "if doc_type ==" not in src

    def test_single_phase1_prompt_constant(self):
        import invoice_processing.extract.document_extractor as mod

        assert hasattr(mod, "PHASE1_PROMPT")
