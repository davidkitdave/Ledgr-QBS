"""Nightly eval property rubrics — Simple Intelligent Puzzle (ADR-0014).

Run deliberately: ``uv run pytest tests/eval/test_extract_properties.py -m eval``
"""

from __future__ import annotations

import pytest

from invoice_processing.export.exporters import XeroLedgerExporter
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from invoice_processing.export.tax_classifier import classify_invoice


pytestmark = pytest.mark.eval


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
