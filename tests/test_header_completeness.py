"""Tests for invoice header completeness: invoice number / date / due date / Total.

Problem (Task 5): *InvoiceDate / *DueDate / Total come out blank in Xero export when the
extractor returns None for invoice_date or invoice_number. This happens because:
  - The model doesn't know about alternative labels (Bill No, Tax Invoice No, Receipt No, Ref).
  - The model doesn't know how to resolve a date-range / statement period to the issue date.
  - The prompt doesn't explicitly document the due-date fallback rule the exporter applies.

All tests are hermetic — no Gemini / network calls. Fixtures are ExtractedInvoice objects
built in-memory, passed through to_normalized() and XeroLedgerExporter.

TDD: tests were written BEFORE the implementation (red → green).
"""

from __future__ import annotations

import pytest

from invoice_processing.export.exporters import XeroLedgerExporter
from invoice_processing.export.models import NormalizedInvoice
from invoice_processing.extract.invoice_extractor import (
    ExtractedInvoice,
    ExtractedLine,
    _BUNDLE_PROMPT,
    _PROMPT,
    to_normalized,
)


# =========================================================================== #
# Helpers
# =========================================================================== #

def _make_invoice(
    *,
    invoice_number: str = "INV-001",
    invoice_date: str = "2024-05-15",
    due_date: str | None = None,
    total: float = 109.00,
    currency: str = "SGD",
) -> ExtractedInvoice:
    return ExtractedInvoice(
        doc_type="invoice",
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        due_date=due_date,
        currency=currency,
        issuer_name="Test Vendor Pte Ltd",
        bill_to_name="Acme Corp",
        lines=[
            ExtractedLine(
                description="Professional services",
                net_amount=100.00,
                gst_amount=9.00,
                tax_label="SR",
            )
        ],
        subtotal=100.00,
        gst_total=9.00,
        total=total,
    )


def _xero_rows(inv: NormalizedInvoice, doc_type: str = "purchase") -> list[dict]:
    exporter = XeroLedgerExporter()
    return exporter.rows([inv], doc_type)


# =========================================================================== #
# A — Xero exporter: *InvoiceDate / *DueDate / Total are non-blank when fields set
# =========================================================================== #

class TestXeroHeaderFields:
    """When ExtractedInvoice has date + number + total, Xero export rows must be non-blank."""

    def test_invoice_date_maps_to_xero_invoice_date(self):
        """invoice_date 2024-05-15 → *InvoiceDate = '15/05/2024' (non-blank)."""
        ex = _make_invoice(invoice_date="2024-05-15")
        inv = to_normalized(ex, direction="purchase", base_currency="SGD")
        rows = _xero_rows(inv)
        assert len(rows) >= 1
        assert rows[0]["*InvoiceDate"] == "15/05/2024", (
            f"Expected '15/05/2024', got '{rows[0]['*InvoiceDate']}'"
        )

    def test_invoice_date_blank_when_none(self):
        """invoice_date=None → *InvoiceDate is blank (empty string)."""
        ex = _make_invoice(invoice_date=None)
        inv = to_normalized(ex, direction="purchase", base_currency="SGD")
        rows = _xero_rows(inv)
        assert rows[0]["*InvoiceDate"] == ""

    def test_invoice_number_maps_to_xero_invoice_number(self):
        """invoice_number 'INV-2024-001' → *InvoiceNumber = 'INV-2024-001' (non-blank)."""
        ex = _make_invoice(invoice_number="INV-2024-001")
        inv = to_normalized(ex, direction="purchase", base_currency="SGD")
        rows = _xero_rows(inv)
        assert rows[0]["*InvoiceNumber"] == "INV-2024-001"

    def test_invoice_number_blank_when_none(self):
        """invoice_number=None → *InvoiceNumber is blank."""
        ex = _make_invoice(invoice_number=None)
        inv = to_normalized(ex, direction="purchase", base_currency="SGD")
        rows = _xero_rows(inv)
        assert rows[0]["*InvoiceNumber"] == ""

    def test_total_is_non_zero_when_doc_has_total(self):
        """doc_total=109.00 → Xero Total column is 109.0 (not 0 or blank)."""
        ex = _make_invoice(total=109.00)
        inv = to_normalized(ex, direction="purchase", base_currency="SGD")
        rows = _xero_rows(inv)
        assert rows[0]["Total"] == pytest.approx(109.00)

    def test_total_falls_back_to_line_sum_when_doc_total_none(self):
        """If doc_total is None, Total = Σ(line net + line tax) — not blank."""
        ex = _make_invoice(total=None)
        inv = to_normalized(ex, direction="purchase", base_currency="SGD")
        # doc_total absent → _invoice_total falls back to Σ lines
        rows = _xero_rows(inv)
        # 100 net + 9 gst = 109 (tax computed for SR line)
        assert rows[0]["Total"] == pytest.approx(109.00, abs=0.10)


# =========================================================================== #
# B — Due-date fallback: *DueDate falls back to *InvoiceDate when due_date is None
# =========================================================================== #

class TestXeroDueDateFallback:
    """When no explicit due date, Xero *DueDate falls back to invoice_date (not blank)."""

    def test_due_date_explicit_maps_correctly(self):
        """Explicit due_date 2024-06-15 → *DueDate = '15/06/2024'."""
        ex = _make_invoice(invoice_date="2024-05-15", due_date="2024-06-15")
        inv = to_normalized(ex, direction="purchase", base_currency="SGD")
        rows = _xero_rows(inv)
        assert rows[0]["*DueDate"] == "15/06/2024"

    def test_due_date_none_falls_back_to_invoice_date(self):
        """due_date=None → *DueDate falls back to invoice_date (15/05/2024), not blank."""
        ex = _make_invoice(invoice_date="2024-05-15", due_date=None)
        inv = to_normalized(ex, direction="purchase", base_currency="SGD")
        rows = _xero_rows(inv)
        # Exporter must not emit blank — it must use invoice_date as fallback
        assert rows[0]["*DueDate"] == "15/05/2024", (
            f"Expected due_date fallback to invoice_date '15/05/2024', got '{rows[0]['*DueDate']}'"
        )

    def test_due_date_blank_only_when_both_dates_none(self):
        """Only when both due_date and invoice_date are None should *DueDate be blank."""
        ex = _make_invoice(invoice_date=None, due_date=None)
        inv = to_normalized(ex, direction="purchase", base_currency="SGD")
        rows = _xero_rows(inv)
        assert rows[0]["*DueDate"] == ""

    def test_due_date_fallback_works_for_sales_too(self):
        """Same fallback applies on the sales path."""
        ex = _make_invoice(invoice_date="2024-05-15", due_date=None)
        inv = to_normalized(ex, direction="sales", base_currency="SGD")
        rows = _xero_rows(inv, doc_type="sales")
        assert rows[0]["*DueDate"] == "15/05/2024"


# =========================================================================== #
# C — Date-range / statement period: issue date is used, not the range start
# =========================================================================== #

class TestDateRangeResolution:
    """When a document shows a statement period / date range, the model must use the
    issue/document date (not the period start). These tests validate that to_normalized()
    correctly parses whatever date string the model returns after prompt clarification."""

    def test_issue_date_within_range_period_used(self):
        """Invoice with issue date 2024-04-30 (end of billing period) maps correctly."""
        # Simulates: billing period 01/04/2024 – 30/04/2024, document date 30/04/2024.
        # The model should return invoice_date = "2024-04-30" (the issue date, not 2024-04-01).
        ex = _make_invoice(invoice_date="2024-04-30")
        inv = to_normalized(ex, direction="purchase", base_currency="SGD")
        assert inv.invoice_date is not None
        assert str(inv.invoice_date) == "2024-04-30"

    def test_invoice_date_is_single_date_not_range(self):
        """to_normalized / _parse_date must never return a string 'YYYY-MM-DD–YYYY-MM-DD'."""
        ex = _make_invoice(invoice_date="2024-04-30")
        inv = to_normalized(ex, direction="purchase", base_currency="SGD")
        # invoice_date is a datetime.date object (not a string range)
        from datetime import date
        assert isinstance(inv.invoice_date, date)
        # And it's the single issue date
        assert inv.invoice_date.year == 2024
        assert inv.invoice_date.month == 4
        assert inv.invoice_date.day == 30


# =========================================================================== #
# D — Prompt / schema: invoice_number + invoice_date + due_date wording is unambiguous
# =========================================================================== #

class TestPromptHeaderCompleteness:
    """The extraction prompt and ExtractedInvoice schema must give unambiguous guidance
    so the model reliably captures invoice number, invoice date, and due date.

    These tests are RED until the prompt/schema is updated (Task 5).
    """

    def test_prompt_mentions_alternative_number_labels(self):
        """Prompt must list alternative labels for invoice number so the model knows
        'Bill No', 'Tax Invoice No', 'Receipt No', and 'Ref' all map to invoice_number."""
        prompt_lower = _PROMPT.lower()
        # At least two of the key alternative labels must be present
        alt_labels = ["bill no", "tax invoice", "receipt no", "ref"]
        found = [lbl for lbl in alt_labels if lbl in prompt_lower]
        assert len(found) >= 2, (
            f"Prompt must mention alternative invoice-number labels (bill no, tax invoice no, "
            f"receipt no, ref). Found only: {found}. Full check on: {alt_labels}"
        )

    def test_prompt_instructs_issue_date_not_range_start(self):
        """Prompt must explicitly tell the model to use the document/issue date, not
        the period start, when the document shows a date range / statement period."""
        prompt_lower = _PROMPT.lower()
        has_issue_date_instruction = (
            "issue date" in prompt_lower
            or "document date" in prompt_lower
            or "statement period" in prompt_lower
            or "date range" in prompt_lower
            or "billing period" in prompt_lower
        )
        assert has_issue_date_instruction, (
            "Prompt must instruct the model to use the document/issue date (not range start) "
            "when the document shows a date range / statement period"
        )

    def test_prompt_mentions_due_date_fallback_rule(self):
        """Prompt must document the due-date fallback: leave null only when neither a
        due date nor payment terms are present (the exporter will then use invoice_date)."""
        prompt_lower = _PROMPT.lower()
        # Must mention the fallback or the null-only-when-absent rule
        has_fallback_instruction = (
            "leave null" in prompt_lower
            or "null only" in prompt_lower
            or "null when" in prompt_lower
            or ("null" in prompt_lower and "due" in prompt_lower)
        )
        assert has_fallback_instruction, (
            "Prompt must document when due_date may be null (and hint that the exporter "
            "will apply the invoice_date fallback)"
        )

    def test_schema_invoice_number_description_mentions_alternatives(self):
        """ExtractedInvoice.invoice_number field description must mention alternative labels."""
        from invoice_processing.extract.invoice_extractor import ExtractedInvoice
        schema = ExtractedInvoice.model_json_schema()
        props = schema.get("properties", {})
        num_prop = props.get("invoice_number", {})
        description = (num_prop.get("description") or "").lower()
        alt_labels = ["bill no", "tax invoice", "receipt no", "ref"]
        found = [lbl for lbl in alt_labels if lbl in description]
        assert len(found) >= 2, (
            f"invoice_number field description must mention alternative labels "
            f"(bill no, tax invoice no, receipt no, ref). Found: {found}"
        )

    def test_schema_invoice_date_description_mentions_issue_date(self):
        """ExtractedInvoice.invoice_date field description must clarify 'issue date,
        not period start' for date-range documents."""
        from invoice_processing.extract.invoice_extractor import ExtractedInvoice
        schema = ExtractedInvoice.model_json_schema()
        props = schema.get("properties", {})
        date_prop = props.get("invoice_date", {})
        description = (date_prop.get("description") or "").lower()
        has_issue_or_doc = (
            "issue" in description
            or "document" in description
        )
        assert has_issue_or_doc, (
            f"invoice_date field description must say 'issue date' or 'document date'. Got: '{description}'"
        )
        # Must also clarify: not the range/period start
        has_not_range = (
            "not" in description
            or "range" in description
            or "period" in description
        )
        assert has_not_range, (
            f"invoice_date field description must clarify 'not the period/range start'. Got: '{description}'"
        )

    def test_schema_due_date_description_mentions_fallback(self):
        """ExtractedInvoice.due_date field description must document when null is acceptable."""
        from invoice_processing.extract.invoice_extractor import ExtractedInvoice
        schema = ExtractedInvoice.model_json_schema()
        props = schema.get("properties", {})
        dd_prop = props.get("due_date", {})
        description = (dd_prop.get("description") or "").lower()
        has_null_rule = (
            "null" in description
            or "absent" in description
            or "neither" in description
            or "leave" in description
        )
        assert has_null_rule, (
            f"due_date description must say when null is acceptable. Got: '{description}'"
        )

    def test_bundle_prompt_inherits_date_and_number_instructions(self):
        """The bundle prompt must also carry date/number instructions (it composes _PROMPT)."""
        bundle_lower = _BUNDLE_PROMPT.lower()
        # Bundle prompt composes _PROMPT so all its instructions should be present
        assert "invoice_date" in bundle_lower or "issue date" in bundle_lower or "document date" in bundle_lower, (
            "Bundle prompt must include invoice_date/issue date instructions"
        )
        alt_labels = ["bill no", "tax invoice", "receipt no", "ref"]
        found = [lbl for lbl in alt_labels if lbl in bundle_lower]
        assert len(found) >= 2, (
            f"Bundle prompt must include invoice number alternative labels. Found: {found}"
        )
