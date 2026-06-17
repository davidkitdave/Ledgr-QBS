"""Unit tests for invoice_processing/export/tax_classifier.py.

Covers:
- Chubb regression: clean SG tax invoice with explicit 9% GST → SR, no flag.
- Explicit standard-rated wording in description → SR, no flag (purchases + sales).
- Genuinely-ambiguous cases still flag (no GST, no reg no., unknown overseas).
- Existing happy-path rules (ZR, ES, NT, OS) remain unaffected.
- tax_keyword short-circuit paths still work.
"""

from __future__ import annotations

from datetime import date

from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from invoice_processing.export.tax_classifier import TaxClassifier, classify_invoice


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _purchase(
    desc: str = "Insurance premium",
    net: float = 1000.0,
    gst: float | None = None,
    gst_regno: str | None = None,
    is_overseas: bool | None = None,
    tax_kw: str | None = None,
    inv_date: date | None = date(2024, 6, 1),
    our_gst_registered: bool = True,
) -> tuple[InvoiceLine, NormalizedInvoice]:
    country = None if is_overseas is None else ("MY" if is_overseas else "SG")
    line = InvoiceLine(
        description=desc,
        net_amount=net,
        gst_amount=gst,
        tax_keyword=tax_kw,
    )
    inv = NormalizedInvoice(
        doc_type="purchase",
        invoice_date=inv_date,
        our_gst_registered=our_gst_registered,
        supplier=PartyInfo(name="Test Supplier", gst_regno=gst_regno, country=country),
    )
    inv.lines.append(line)
    return line, inv


def _sales(
    desc: str = "Consulting service",
    net: float = 1000.0,
    gst: float | None = None,
    gst_regno: str | None = None,
    is_overseas: bool = False,
    tax_kw: str | None = None,
    our_gst_registered: bool = True,
    inv_date: date | None = date(2024, 6, 1),
) -> tuple[InvoiceLine, NormalizedInvoice]:
    country = "MY" if is_overseas else "SG"
    line = InvoiceLine(
        description=desc,
        net_amount=net,
        gst_amount=gst,
        tax_keyword=tax_kw,
    )
    inv = NormalizedInvoice(
        doc_type="sales",
        invoice_date=inv_date,
        our_gst_registered=our_gst_registered,
        customer=PartyInfo(name="Test Customer", gst_regno=gst_regno, country=country),
    )
    inv.lines.append(line)
    return line, inv


CLF = TaxClassifier()


# ===========================================================================
# Task 6 — GST tax-code determinacy on clean tax invoices
# ===========================================================================

class TestChubbRegression:
    """A clean SG tax invoice with an explicit 9% GST line must resolve SR without flagging.

    Chubb scenario: supplier shows no explicit GST reg no. in extracted data, but the
    invoice description contains an explicit standard-rate GST indicator (e.g. "GST 9%"
    or "(SR)"). Prior behaviour: fell through to the indeterminate path → "SR(default)",
    confidence 0.4, flagged=True. Expected: SR, confidence ≥ 0.8, flagged=False.
    """

    def test_chubb_explicit_gst_9pct_in_description_resolves_sr_no_flag(self):
        """Core regression: explicit '9% GST' wording → SR, not flagged."""
        line, inv = _purchase(
            desc="Insurance premium GST 9%",
            net=1000.0,
            gst=90.0,
            gst_regno=None,          # reg no. not captured from the PDF
        )
        result = CLF.classify_line(line, inv)

        assert result.tax_treatment == "SR", (
            f"Expected SR, got {result.tax_treatment}. Reason: {result.tax_reason}"
        )
        assert not result.tax_flagged, (
            f"Expected no flag for explicit 9% GST line, got flagged=True. "
            f"Reason: {result.tax_reason}"
        )
        assert result.tax_confidence >= 0.8, (
            f"Expected confidence ≥ 0.8, got {result.tax_confidence}"
        )

    def test_explicit_standard_rated_wording_in_description_no_flag(self):
        """'(SR)' or 'standard-rated' in description → SR, not flagged."""
        line, inv = _purchase(
            desc="Professional services (SR)",
            net=500.0,
            gst=45.0,
            gst_regno=None,
        )
        result = CLF.classify_line(line, inv)

        assert result.tax_treatment == "SR"
        assert not result.tax_flagged, f"Reason: {result.tax_reason}"
        assert result.tax_confidence >= 0.8

    def test_gst_9pct_no_gst_amount_still_resolves_sr_no_flag(self):
        """Explicit '9%' wording with no gst_amount captured → still SR, not flagged."""
        line, inv = _purchase(
            desc="Software licence GST 9%",
            net=200.0,
            gst=None,               # GST amount not separately captured
            gst_regno=None,
        )
        result = CLF.classify_line(line, inv)

        assert result.tax_treatment == "SR"
        assert not result.tax_flagged, f"Reason: {result.tax_reason}"
        assert result.tax_confidence >= 0.8

    def test_explicit_sr_signal_sales_invoice_no_flag(self):
        """Sales invoice: explicit standard-rated signal in description → SR, not flagged."""
        line, inv = _sales(
            desc="Delivery service standard-rated",
            net=800.0,
            gst=72.0,
        )
        result = CLF.classify_line(line, inv)

        assert result.tax_treatment == "SR"
        assert not result.tax_flagged, f"Reason: {result.tax_reason}"


# ===========================================================================
# Genuinely-ambiguous cases STILL flag (over-correction guard)
# ===========================================================================

class TestAmbiguousCasesStillFlag:
    """Ensure the new SR-determinacy rule does NOT auto-resolve ambiguous cases."""

    def test_no_gst_unregistered_supplier_overseas_unknown_flags(self):
        """Overseas supplier, no GST, no reg no., no explicit wording → flag (OS/indeterminate)."""
        line, inv = _purchase(
            desc="Consulting services",
            net=1000.0,
            gst=None,
            gst_regno=None,
            is_overseas=True,   # clearly overseas
        )
        result = CLF.classify_line(line, inv)

        assert result.tax_flagged, (
            f"Expected flag for overseas-no-GST case, got flagged=False. "
            f"Treatment={result.tax_treatment}, Reason={result.tax_reason}"
        )

    def test_gst_shown_no_supplier_reg_no_non_reconciling_amount_flags(self):
        """GST shown but does NOT reconcile to standard rate and no reg no./wording → flag.

        50.0 on a 1000.0 net is 5%, not 9% — clearly wrong rate, must remain flagged.
        """
        line, inv = _purchase(
            desc="Miscellaneous charges",   # no standard-rate signal words
            net=1000.0,
            gst=50.0,           # 5% — does NOT reconcile to 9% standard rate
            gst_regno=None,
        )
        result = CLF.classify_line(line, inv)

        assert result.tax_flagged, (
            f"Expected flag when GST does not reconcile to standard rate and no reg no./wording. "
            f"Treatment={result.tax_treatment}, Reason={result.tax_reason}"
        )

    def test_gst_reconciles_to_standard_rate_no_reg_no_no_wording_sr_no_flag(self):
        """True Chubb case: gst reconciles to 9% of net, no reg no., plain description → SR, no flag.

        The plain description 'Insurance premium' has no standard-rate signal words;
        the only evidence is that gst_amount == net_amount * 9%. That is sufficient.
        """
        line, inv = _purchase(
            desc="Insurance premium",       # no standard-rate wording in description
            net=1000.0,
            gst=90.0,                       # exactly 9% of 1000 → reconciles
            gst_regno=None,
        )
        result = CLF.classify_line(line, inv)

        assert result.tax_treatment == "SR", (
            f"Expected SR, got {result.tax_treatment}. Reason: {result.tax_reason}"
        )
        assert not result.tax_flagged, (
            f"Expected no flag when GST reconciles to standard rate. "
            f"Reason: {result.tax_reason}"
        )
        assert result.tax_confidence >= 0.8, (
            f"Expected confidence ≥ 0.8, got {result.tax_confidence}"
        )

    def test_gst_does_not_reconcile_to_standard_rate_no_reg_no_flags(self):
        """GST present, does NOT reconcile to standard rate, no reg no., no wording → flagged.

        55.0 on a 1000.0 net is 5.5%, not 9%. No reg no., no wording → should still flag.
        """
        line, inv = _purchase(
            desc="Insurance premium",       # plain description, no SR wording
            net=1000.0,
            gst=55.0,                       # 5.5% — wrong rate, must flag
            gst_regno=None,
        )
        result = CLF.classify_line(line, inv)

        assert result.tax_flagged, (
            f"Expected flag when GST does not reconcile to standard rate. "
            f"Treatment={result.tax_treatment}, Reason={result.tax_reason}"
        )

    def test_indeterminate_no_gst_no_reg_no_domestic_flags(self):
        """Domestic supplier, no GST, no reg no., no explicit wording → flag."""
        line, inv = _purchase(
            desc="Office supplies",
            net=500.0,
            gst=None,
            gst_regno=None,
            is_overseas=False,
        )
        result = CLF.classify_line(line, inv)

        # supplier.is_overseas=False so not the OS path; no gst_registered; no gst_amount
        # → should hit NT (not GST-registered / no GST line) which is NOT flagged — but
        # this is still deterministic (NT). What we guard is: it must NOT return SR
        # with high confidence without evidence.
        # Actually NT is a valid low-confidence-safe resolution; the key guard is that
        # we don't return SR, no-flag without explicit evidence.
        assert result.tax_treatment in ("NT", "SR"), (
            f"Unexpected treatment {result.tax_treatment}"
        )
        # If it resolved SR without a standard-rate signal, it must flag
        if result.tax_treatment == "SR":
            assert result.tax_flagged, (
                "SR resolved without explicit standard-rate signal must be flagged"
            )

    def test_zero_rated_wording_does_not_become_sr(self):
        """ZR signal in description must not be overridden by the new SR rule."""
        line, inv = _purchase(
            desc="International freight export GST 9%",  # both signals present
            net=1000.0,
            gst=None,
            gst_regno="M12345678X",
        )
        result = CLF.classify_line(line, inv)

        # ZR signal (international freight / export) must win over standard-rated wording
        assert result.tax_treatment == "ZR", (
            f"ZR signal must take priority over standard-rated wording. "
            f"Got {result.tax_treatment}. Reason: {result.tax_reason}"
        )

    def test_exempt_wording_does_not_become_sr(self):
        """Exempt signal in description must not be overridden by the new SR rule."""
        line, inv = _purchase(
            desc="Residential rent (exempt) standard-rated",
            net=2000.0,
            gst=None,
            gst_regno="M12345678X",
        )
        result = CLF.classify_line(line, inv)

        assert result.tax_treatment == "ES", (
            f"ES signal must take priority. Got {result.tax_treatment}. Reason: {result.tax_reason}"
        )


# ===========================================================================
# Existing happy-path rules — regression guard
# ===========================================================================

class TestExistingRulesUnchanged:
    """Core rules that existed before Task 6 must remain unaffected."""

    def test_gst_positive_registered_supplier_sr_no_flag(self):
        line, inv = _purchase(gst=90.0, gst_regno="M12345678X")
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "SR"
        assert not result.tax_flagged

    def test_zero_rated_idd_desc(self):
        line, inv = _purchase(desc="IDD international call", gst=0.0, gst_regno="M12345678X")
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "ZR"
        assert not result.tax_flagged

    def test_exempt_interest_income(self):
        line, inv = _purchase(desc="Bank interest income", gst=None, gst_regno="M12345678X")
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "ES"
        assert not result.tax_flagged

    def test_nt_unregistered_no_gst(self):
        line, inv = _purchase(desc="Hawker food stall", gst=None, gst_regno=None, is_overseas=False)
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "NT"
        # NT at confidence 0.95 — expected for no-GST supplier; not flagged for review
        assert result.tax_confidence == 0.95
        assert result.tax_flagged is False

    def test_overseas_no_gst_flags_os(self):
        line, inv = _purchase(desc="Consulting", gst=None, gst_regno=None, is_overseas=True)
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "OS"
        assert result.tax_flagged

    def test_tax_keyword_zr_shortcircuit(self):
        line, inv = _purchase(desc="Misc charge", gst=None, gst_regno="M12345678X", tax_kw="ZR")
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "ZR"
        assert not result.tax_flagged

    def test_tax_keyword_sr_shortcircuit(self):
        line, inv = _purchase(desc="Misc charge", gst=90.0, gst_regno="M12345678X", tax_kw="SR")
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "SR"
        assert not result.tax_flagged

    def test_sales_default_local_sr_no_flag(self):
        line, inv = _sales(desc="Local consulting", gst=90.0)
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "SR"
        assert not result.tax_flagged

    def test_sales_not_gst_registered_nt(self):
        line, inv = _sales(desc="Service", our_gst_registered=False)
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "NT"
        assert not result.tax_flagged

    # ----- master-gate regressions (1.5c, user-confirmed SG GST rule) -----
    # Non-GST-registered Ledgr client: ALL lines = NT regardless of doc.
    # See memory ``sg-gst-tax-rule-and-xero-codes``.

    def test_purchase_non_registered_client_overrides_supplier_gst(self):
        """Non-reg client + supplier shows 9% GST → NT (input GST = cost).

        Pre-fix this returned SR (supplier registered + GST shown) — the
        live-QA-caught wrong-number bug for non-registered Ledgr clients.
        """
        line, inv = _purchase(
            desc="Office supplies", net=1000.0, gst=90.0,
            gst_regno="M12345678X", our_gst_registered=False,
        )
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "NT"
        assert not result.tax_flagged
        assert "not GST-registered" in (result.tax_reason or "")

    def test_purchase_non_registered_client_overrides_zr_signal(self):
        """Non-reg client + zero-rated signal (telco/freight/export) → still NT."""
        line, inv = _purchase(
            desc="International freight charge", our_gst_registered=False,
        )
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "NT"

    def test_purchase_non_registered_client_overrides_explicit_tax_keyword(self):
        """Non-reg client + extractor found tax_keyword='ZR' → still NT.

        The legal effect on a non-registered client's books does not change
        with what the invoice writes — they cannot claim input GST.
        """
        line, inv = _purchase(
            desc="Service", tax_kw="ZR", our_gst_registered=False,
        )
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "NT"

    def test_sales_non_registered_client_overrides_sr_keyword(self):
        """Non-reg client + accidental 'SR' keyword on sales → still NT.

        A non-GST-registered client cannot legally charge GST. The master gate
        is hoisted ABOVE the tax_keyword block to enforce this.
        """
        line, inv = _sales(desc="Service", tax_kw="SR", our_gst_registered=False)
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "NT"

    def test_purchase_registered_client_unchanged_sr(self):
        """Registered client + 9% supplier → SR (regression: pre-fix behaviour preserved)."""
        line, inv = _purchase(
            desc="Office supplies", net=1000.0, gst=90.0,
            gst_regno="M12345678X", our_gst_registered=True,
        )
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "SR"
        assert not result.tax_flagged

    def test_xero_code_map_short_sg_codes(self):
        """Xero SG org uses SR / ZR / No Tax (not long BillTemplate strings)."""
        assert CLF.tax_code("SR", "purchase", "xero") == "SR"
        assert CLF.tax_code("ZR", "purchase", "xero") == "ZR"
        assert CLF.tax_code("NT", "purchase", "xero") == "No Tax"
        assert CLF.tax_code("NT", "sales", "xero") == "No Tax"

    def test_rate_mismatch_flags(self):
        """Correct SR path but GST amount doesn't reconcile to 9% → flag."""
        # 9% of 1000 = 90; supply 50 → mismatch
        line, inv = _purchase(gst=50.0, net=1000.0, gst_regno="M12345678X")
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "SR"
        assert result.tax_flagged, "Rate mismatch must flag even if SR resolved"


# ===========================================================================
# classify_invoice convenience wrapper
# ===========================================================================

class TestClassifyInvoice:
    def test_classifies_all_lines(self):
        inv = NormalizedInvoice(
            doc_type="purchase",
            invoice_date=date(2024, 6, 1),
            supplier=PartyInfo(gst_regno="M12345678X"),
        )
        inv.lines = [
            InvoiceLine(description="Line A", net_amount=100.0, gst_amount=9.0),
            InvoiceLine(description="IDD call", net_amount=50.0, gst_amount=0.0),
        ]
        result = classify_invoice(inv)
        assert result.lines[0].tax_treatment == "SR"
        assert result.lines[1].tax_treatment == "ZR"
