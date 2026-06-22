"""Tests for DocumentRecord schema and Phase 1/2 modules (hermetic — no network)."""

from __future__ import annotations

import pytest

from invoice_processing.extract.document_extractor import PHASE1_PROMPT
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


class TestAntiHardcode:
    def test_no_expense_claim_doc_type_branch_in_extractor(self):
        import invoice_processing.extract.document_extractor as mod

        src = open(mod.__file__).read()
        assert "expense_claim" not in src
        assert "if doc_type ==" not in src

    def test_single_phase1_prompt_constant(self):
        import invoice_processing.extract.document_extractor as mod

        assert hasattr(mod, "PHASE1_PROMPT")
