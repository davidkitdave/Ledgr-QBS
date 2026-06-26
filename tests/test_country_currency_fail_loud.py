"""Issue #27 — no silent SG/SGD defaults; fail loud on missing country/currency.

A document/profile that does not expose a country or currency must NOT silently
become Singapore / SGD. The missing value stays explicit (None / "") so the
accountant records it as genuinely unknown, and — where a tax-regime decision
actually depends on it — the document routes to Review (HITL) instead of
proceeding on a guessed default.

Counterpart: documents that DO show country/currency are unaffected (no new
pauses, no value rewrite). Those guarantees are exercised by the wider suite and
re-asserted here for the present case so the fail-loud change cannot regress
record-as-shown behaviour.
"""

from __future__ import annotations

from accounting_agents.jurisdiction import resolve_jurisdiction
from accounting_agents.normalized_invoice_codec import dict_to_invoice
from invoice_processing.extract.invoice_extractor import ExtractedInvoice, to_normalized
from invoice_processing.extract.ledger_extract import (
    ExtractedDocument,
    ExtractedDocumentLine,
    extracted_document_to_normalized,
)


def _currencyless_extracted_invoice() -> ExtractedInvoice:
    """An ExtractedInvoice whose document prints no currency at all."""
    return ExtractedInvoice(
        doc_type="purchase",
        invoice_number="INV-NOCCY",
        currency=None,
        issuer_name="Anon Supplier Pte Ltd",
        issuer_tax_system="NONE",
        lines=[],
        subtotal=100.0,
        gst_total=0.0,
        total=100.0,
    )


def _countryless_doc() -> ExtractedDocument:
    """A faithful document with no visible vendor/buyer country and no currency."""
    return ExtractedDocument(
        doc_type="invoice",
        page_range=[1, 1],
        vendor="Anon Vendor",
        reference="INV-NOCOUNTRY",
        date="2026-06-26",
        currency="",  # ExtractedDocument represents "no currency printed" as "" (not None)
        vendor_country=None,
        buyer_country=None,
        grand_total=100.0,
        subtotal=100.0,
        tax_total=0.0,
        tax_visible_on_document=False,
        lines=[
            ExtractedDocumentLine(description="Service", net_amount=100.0, gst_amount=0.0),
        ],
    )


# --------------------------------------------------------------------------- #
# AC2 — no visible currency must not silently become SGD
# --------------------------------------------------------------------------- #
def test_currencyless_doc_does_not_silently_become_sgd_in_to_normalized():
    """No document currency AND no client base currency → explicit-unknown, not SGD."""
    ex = _currencyless_extracted_invoice()
    inv = to_normalized(ex, direction="purchase")  # caller supplies no base_currency
    assert inv.currency != "SGD", (
        "currency-less document silently defaulted to SGD; "
        "it must stay explicit-unknown (record as shown, no conversion)"
    )
    assert inv.currency == ""


def test_currencyless_doc_does_not_silently_become_sgd_via_ledger_mapper():
    """The faithful-array mapper path must not fabricate SGD either."""
    doc = _countryless_doc()
    inv = extracted_document_to_normalized(doc, direction="purchase")
    assert inv.currency != "SGD"
    assert inv.currency == ""


def test_currencyless_dict_rehydrates_unknown_not_sgd():
    """Re-hydrating a stored doc with no currency must not invent SGD."""
    inv = dict_to_invoice({"invoice_number": "INV-X", "doc_type": "purchase"})
    assert inv.currency != "SGD"
    assert inv.currency == ""


def test_present_currency_is_preserved_unchanged():
    """Counterpart: a document that DOES print a currency is recorded as shown."""
    ex = _currencyless_extracted_invoice()
    ex = ex.model_copy(update={"currency": "USD"})
    inv = to_normalized(ex, direction="purchase")
    assert inv.currency == "USD"
    # A stored dict carrying a currency round-trips faithfully.
    assert dict_to_invoice({"currency": "MYR"}).currency == "MYR"


# --------------------------------------------------------------------------- #
# AC1 — no visible country must not silently become SG, and where a tax-regime
#       decision depends on it, route to Review (the existing HITL signal).
# --------------------------------------------------------------------------- #
def test_countryless_client_routes_to_review_not_silent_sg():
    """No client region in state → AMBIGUOUS + flag_for_human, never a silent SG rule."""
    res = resolve_jurisdiction({})  # nothing supplied: no region, no currency, no parties
    assert res.jurisdiction.flag_for_human is True, (
        "missing client country/region must route to Review, not pick SG silently"
    )
    assert res.jurisdiction.tax_system != "GST"
    assert res.client_region == ""


def test_present_country_picks_its_regime_without_new_pause():
    """Counterpart: a client that DOES declare its region resolves cleanly, no flag."""
    sg = resolve_jurisdiction({"region": "SINGAPORE"})
    assert sg.jurisdiction.tax_system == "GST"
    assert sg.jurisdiction.flag_for_human is False

    my = resolve_jurisdiction({"region": "MALAYSIA"})
    assert my.jurisdiction.tax_system == "SST"
    assert my.jurisdiction.flag_for_human is False
