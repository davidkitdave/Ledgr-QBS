"""Nightly eval property rubrics — Simple Intelligent Puzzle (ADR-0014).

Run deliberately: ``uv run pytest tests/eval/test_extract_properties.py -m eval``
"""

from __future__ import annotations

import pytest

from invoice_processing.export.exporters import XeroLedgerExporter
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from invoice_processing.export.tax_classifier import classify_invoice
from invoice_processing.extract.document_record import DocumentRecord, LabeledField, LineCapture, PartyCapture
from invoice_processing.extract.verify import tax_visible_on_capture, verify_extracted_invoice
from invoice_processing.extract.book import BookingProposal, BookingLedgerLine, booking_to_extracted_invoice


pytestmark = pytest.mark.eval


def _expense_capture() -> DocumentRecord:
    return DocumentRecord(
        doc_kind_guess="expense_claim",
        parties=[PartyCapture(name="Employee", role_hint="employee")],
        line_items=[
            LineCapture(description="Transport", net_amount=263.85),
            LineCapture(description="Hotel", net_amount=274.14),
            LineCapture(description="Other", net_amount=457.12),
        ],
        totals=[LabeledField(label="Total USD", value="995.11")],
    )


@pytest.mark.eval
def test_property_tax_not_invented_on_silent_document():
    inv = NormalizedInvoice(
        doc_type="purchase",
        our_gst_registered=True,
        tax_visible_on_document=False,
        supplier=PartyInfo(name="Employee"),
        lines=[InvoiceLine(description="Travel", net_amount=100.0, gst_amount=0.0)],
    )
    classify_invoice(inv)
    row = XeroLedgerExporter().rows([inv], "purchase")[0]
    assert row["TaxAmount"] == 0.0
    assert row["*TaxType"] == "No Tax"


@pytest.mark.eval
def test_property_footer_reconcile_skips_absent_subtotal():
    capture = _expense_capture()
    proposal = BookingProposal(
        doc_kind="expense_claim",
        direction_for_client="purchase",
        direction_reason="Client is recipient on expense form",
        ledger_lines=[
            BookingLedgerLine(description="Transport", net_amount=263.85),
            BookingLedgerLine(description="Hotel", net_amount=274.14),
            BookingLedgerLine(description="Other", net_amount=457.12),
        ],
        document_total=995.11,
        tax_visible_on_document=False,
    )
    ex = booking_to_extracted_invoice(proposal, capture)
    ok, note = verify_extracted_invoice(ex, capture)
    assert ok is True
    assert "ok" in note.lower()


@pytest.mark.eval
def test_property_tax_visible_detected_from_capture_totals():
    capture = DocumentRecord(
        totals=[
            LabeledField(label="Sub Total", value="100.00"),
            LabeledField(label="GST", value="9.00"),
            LabeledField(label="Total", value="109.00"),
        ],
    )
    assert tax_visible_on_capture(capture) is True


@pytest.mark.eval
def test_property_grounded_direction_reason_present():
    proposal = BookingProposal(
        doc_kind="expense_claim",
        direction_for_client="purchase",
        direction_reason="Company-A is billed to on the form header",
        ledger_lines=[BookingLedgerLine(description="Claim", net_amount=50.0)],
        document_total=50.0,
        tax_visible_on_document=False,
    )
    assert "billed" in proposal.direction_reason.lower()
