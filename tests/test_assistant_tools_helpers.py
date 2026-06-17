"""Unit tests for assistant_tools helpers."""

from accounting_agents.assistant_tools._helpers import filename_matches_query, row_search_text


def test_filename_matches_query_partial_pdf_name():
    assert filename_matches_query("25-D15", "25-D15-Podaima Paid.pdf")


def test_filename_matches_query_xero_prefix():
    assert filename_matches_query("25-D15", "Xero:25-D15")


def test_row_search_text_includes_invoice_number():
    text = row_search_text({"*InvoiceNumber": "25-D12", "*ContactName": "Darrell Podaima"})
    assert "25-d12" in text
    assert "darrell" in text
