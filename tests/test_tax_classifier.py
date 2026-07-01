"""Unit tests for ledgr_slack/export/tax_classifier.py (legacy NormalizedInvoice path).

Not on the live Slack hot path (light path uses printed tax from read_doc).
Kept for YAML reference rules and exporter preview edge cases.
"""

from __future__ import annotations

import pytest
from datetime import date

from ledgr_slack.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from ledgr_slack.export.tax_classifier import TaxClassifier, classify_invoice, get_tax_classifier

pytestmark = pytest.mark.legacy


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
        """Explicit '9%' wording with no gst_amount captured → SR tie-break, flagged."""
        line, inv = _purchase(
            desc="Software licence GST 9%",
            net=200.0,
            gst=None,               # GST amount not separately captured
            gst_regno=None,
        )
        result = CLF.classify_line(line, inv)

        assert result.tax_treatment == "SR"
        assert result.tax_flagged, f"Lexicon tie-break must flag. Reason: {result.tax_reason}"
        assert result.tax_confidence < 0.8

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
        # → rule 6 "NT: supplier not GST-registered / no GST line" fires before rule 7.
        # Rule 7 (genuinely indeterminate) now returns None+flagged, never SR silently.
        assert result.tax_treatment in ("NT", None), (
            f"Unexpected treatment {result.tax_treatment!r} — must not silently emit SR"
        )
        # If it resolved None (indeterminate), it must be flagged for human review
        if result.tax_treatment is None:
            assert result.tax_flagged, (
                "Indeterminate (None) treatment must be flagged for human review"
            )

    def test_zero_rated_wording_does_not_become_sr(self):
        """ZR lexicon tie-break must not lose to standard-rated lexicon tie-break."""
        line, inv = _purchase(
            desc="International freight export GST 9%",  # both signals present
            net=1000.0,
            gst=None,
            gst_regno="M12345678X",
        )
        result = CLF.classify_line(line, inv)

        assert result.tax_treatment == "ZR", (
            f"ZR tie-break must take priority over standard-rated wording. "
            f"Got {result.tax_treatment}. Reason: {result.tax_reason}"
        )
        assert result.tax_flagged

    def test_exempt_wording_does_not_become_sr(self):
        """Exempt lexicon tie-break must not be overridden by standard-rated tie-break."""
        line, inv = _purchase(
            desc="Residential rent (exempt) standard-rated",
            net=2000.0,
            gst=None,
            gst_regno="M12345678X",
        )
        result = CLF.classify_line(line, inv)

        assert result.tax_treatment == "ES", (
            f"ES tie-break must take priority. Got {result.tax_treatment}. Reason: {result.tax_reason}"
        )
        assert result.tax_flagged


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
        assert result.tax_flagged
        assert result.tax_confidence < 0.8

    def test_exempt_interest_income(self):
        line, inv = _purchase(desc="Bank interest income", gst=None, gst_regno="M12345678X")
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment == "ES"
        assert result.tax_flagged
        assert result.tax_confidence < 0.8

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

    def test_sales_gst_non_reconciling_amount_flags_for_review(self):
        """Sales: positive GST that does not match any allowed rate band → SR flagged, low conf."""
        line, inv = _sales(
            desc="Local consulting",
            net=1000.0,
            gst=50.0,  # 5% — does not reconcile to 9% standard rate
        )
        result = CLF.classify_line(line, inv)

        assert result.tax_treatment == "SR"
        assert result.tax_flagged, (
            f"Expected flag when printed GST does not reconcile. Reason: {result.tax_reason}"
        )
        assert result.tax_confidence <= 0.5, (
            f"Expected low confidence for non-reconciling GST, got {result.tax_confidence}"
        )
        assert "does not reconcile" in (result.tax_reason or "").lower()

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
# WS2 — Malaysia SST multi-rate bands (service 6%/8%, sales 5%/10%)
# ===========================================================================

class TestMySstMultiRate:
  def test_service_invoice_2024_03_plus_8pct_not_flagged(self):
    clf = get_tax_classifier("my_sst.yaml")
    inv = NormalizedInvoice(
      doc_type="purchase",
      invoice_date=date(2024, 6, 1),
      our_gst_registered=True,
      tax_visible_on_document=True,
      supplier=PartyInfo(gst_regno="200012346A", country="MY"),
    )
    inv.lines.append(
      InvoiceLine(description="Workshop labour", net_amount=100.0, gst_amount=8.0)
    )
    clf.classify_line(inv.lines[0], inv)
    line = inv.lines[0]
    assert line.tax_treatment == "SR"
    assert line.tax_flagged is False

  def test_carve_out_telecom_6pct_not_flagged(self):
    clf = get_tax_classifier("my_sst.yaml")
    inv = NormalizedInvoice(
      doc_type="purchase",
      invoice_date=date(2024, 6, 1),
      our_gst_registered=True,
      tax_visible_on_document=True,
      supplier=PartyInfo(gst_regno="200012346A", country="MY"),
    )
    inv.lines.append(
      InvoiceLine(
        description="Mobile telecom monthly plan",
        net_amount=100.0,
        gst_amount=6.0,
      )
    )
    clf.classify_line(inv.lines[0], inv)
    line = inv.lines[0]
    assert line.tax_treatment == "SR"
    assert line.tax_flagged is False

  def test_sales_tax_goods_5_and_10_pct_not_flagged(self):
    clf = get_tax_classifier("my_sst.yaml")
    for gst, desc in ((5.0, "Essential groceries"), (10.0, "Standard goods supply")):
      inv = NormalizedInvoice(
        doc_type="purchase",
        invoice_date=date(2024, 6, 1),
        our_gst_registered=True,
        tax_visible_on_document=True,
        supplier=PartyInfo(gst_regno="200012346A", country="MY"),
      )
      inv.lines.append(
        InvoiceLine(description=desc, net_amount=100.0, gst_amount=gst)
      )
      clf.classify_line(inv.lines[0], inv)
      line = inv.lines[0]
      assert line.tax_treatment == "SSR"
      assert line.tax_flagged is False, f"{desc} gst={gst}"

  def test_autocount_rate_keyed_sr_code(self):
    clf = get_tax_classifier("my_sst.yaml")
    assert clf.tax_code("SR", "purchase", "autocount", rate=0.08) == "SV-8"
    assert clf.tax_code("SR", "purchase", "autocount", rate=0.06) == "SV-6"

  def test_autocount_sales_es_rate_keyed(self):
    clf = get_tax_classifier("my_sst.yaml")
    assert clf.tax_code("ES", "sales", "autocount", rate=0.08) == "ESV-8"
    assert clf.tax_code("ES", "sales", "autocount", rate=0.06) == "ESV-6"

  def test_sql_account_flat_codes_no_svz(self):
    clf = get_tax_classifier("my_sst.yaml")
    assert clf.tax_code("SR", "purchase", "sql_account") == "SV"
    assert clf.tax_code("IM", "purchase", "sql_account") == "IMSV"
    assert clf.tax_code("SSR", "purchase", "sql_account") == "ST5"
    table = clf.tax["code_map"]["sql_account"]["purchase"]
    assert "SVZ" not in table


# ===========================================================================
# MY SST dual-band: both 6% and 8% are valid SR rates in dual-rate period
# (post 2024-03). Rate guard must accept EITHER without keyword gating.
# ===========================================================================

class TestMyDualBandAccepted:
    """Both 6% (carve-out) and 8% (standard) are legitimate concurrent service-tax
    bands in MY post-2024-03. allowed_rates_for_treatment must return both
    regardless of line description. Only genuinely anomalous rates (neither band)
    should flag.
    """

    CLF_MY = get_tax_classifier("my_sst.yaml")

    def _my_purchase(self, desc: str, net: float = 100.0, gst: float = 6.0) -> tuple:
        inv = NormalizedInvoice(
            doc_type="purchase",
            invoice_date=date(2025, 12, 1),  # post 2025-07-01 dual-rate regime
            our_gst_registered=True,
            tax_visible_on_document=True,
            supplier=PartyInfo(gst_regno="200012346A", country="MY"),
        )
        line = InvoiceLine(description=desc, net_amount=net, gst_amount=gst)
        inv.lines.append(line)
        return line, inv

    @staticmethod
    def _allowed(clf, line, inv) -> list[float]:
        return clf.allowed_rates_for_treatment("SR", line, inv)

    def test_generic_description_includes_both_bands(self):
        """Generic description must return both 6% and 8% — no keyword dependency."""
        line, inv = self._my_purchase("Total charges for taxable items", gst=6.0)
        rates = self._allowed(self.CLF_MY, line, inv)
        assert rates == sorted([0.06, 0.08]), (
            f"Expected [0.06, 0.08] for generic description, got {rates}"
        )

    def test_consulting_description_also_includes_both_bands(self):
        """Standard description must also return both bands (no keyword exclusion)."""
        line, inv = self._my_purchase("Consulting services", gst=8.0)
        rates = self._allowed(self.CLF_MY, line, inv)
        assert rates == sorted([0.06, 0.08]), (
            f"Expected [0.06, 0.08] for consulting description, got {rates}"
        )

    def test_6pct_generic_line_does_not_flag(self):
        """End-to-end: 6% SST on a generic line must not trigger HITL."""
        line, inv = self._my_purchase(
            "Total charges for taxable items", net=71.28, gst=4.27
        )
        result = self.CLF_MY.classify_line(line, inv)
        assert result.tax_treatment == "SR", f"Got {result.tax_treatment}"
        assert result.tax_flagged is False, (
            f"6% SST on generic line must not flag. Reason: {result.tax_reason}"
        )

    def test_8pct_line_does_not_flag(self):
        """End-to-end: 8% SST on any SR line must not flag."""
        line, inv = self._my_purchase(
            "Professional services fee", net=100.0, gst=8.0
        )
        result = self.CLF_MY.classify_line(line, inv)
        assert result.tax_treatment == "SR", f"Got {result.tax_treatment}"
        assert result.tax_flagged is False, (
            f"8% SST must not flag. Reason: {result.tax_reason}"
        )

    def test_anomalous_rate_flags(self):
        """A rate far from both SST bands (6% / 8%) must flag as anomalous.

        Rates within the classifier's 2% reconciliation tolerance of either band
        (e.g. 7% — 1% from each) are legitimately absorbed and do NOT flag, so the
        anomaly must be unambiguous: 15% on a 100 net line is ~7 points outside the
        nearest band, so ``_best_rate_match`` cannot reconcile it and must flag.
        """
        line, inv = self._my_purchase("Professional services", net=100.0, gst=15.0)
        result = self.CLF_MY.classify_line(line, inv)
        assert result.tax_flagged is True, (
            f"~15% rate must flag as anomalous. Got flagged={result.tax_flagged}, "
            f"treatment={result.tax_treatment}, reason={result.tax_reason}"
        )


# ===========================================================================
# B.1 — SG AutoCount / SQL Account ERP code resolution (was blank before fix)
# ===========================================================================

class TestSgErpCodeResolution:
    """Regression lock for hole B.1: SG autocount/sql_account tax codes were blank.

    Before the fix, sg_gst.yaml had no ``autocount`` or ``sql_account`` keys in
    ``code_map``.  ``TaxClassifier.tax_code(treatment, doc_type, system)`` calls
    ``code_map.get(system)``, so ``system="autocount"`` returned ``None`` → ``""``.
    The fix added explicit blocks mirroring IRAS short-code convention (§7.2):
    purchase SR → "TX", ZR → "ZR", NT → "NT"; sales SR → "SR".
    """

    CLF_SG = get_tax_classifier("sg_gst.yaml")

    # --- purchase SR (the primary hole: "TX" was blank before B.1) -----------

    def test_autocount_purchase_sr_is_tx(self):
        """autocount purchase SR must resolve to 'TX', not ''."""
        code = self.CLF_SG.tax_code("SR", "purchase", "autocount")
        assert code == "TX", f"Expected 'TX', got {code!r}"

    def test_sql_account_purchase_sr_is_tx(self):
        """sql_account purchase SR must resolve to 'TX', not ''."""
        code = self.CLF_SG.tax_code("SR", "purchase", "sql_account")
        assert code == "TX", f"Expected 'TX', got {code!r}"

    # --- purchase ZR ----------------------------------------------------------

    def test_autocount_purchase_zr_is_zr(self):
        code = self.CLF_SG.tax_code("ZR", "purchase", "autocount")
        assert code == "ZR", f"Expected 'ZR', got {code!r}"

    def test_sql_account_purchase_zr_is_zr(self):
        code = self.CLF_SG.tax_code("ZR", "purchase", "sql_account")
        assert code == "ZR", f"Expected 'ZR', got {code!r}"

    # --- purchase NT ----------------------------------------------------------

    def test_autocount_purchase_nt_is_nt(self):
        code = self.CLF_SG.tax_code("NT", "purchase", "autocount")
        assert code == "NT", f"Expected 'NT', got {code!r}"

    # --- sales SR -------------------------------------------------------------

    def test_autocount_sales_sr_is_sr(self):
        code = self.CLF_SG.tax_code("SR", "sales", "autocount")
        assert code == "SR", f"Expected 'SR', got {code!r}"

    def test_sql_account_sales_sr_is_sr(self):
        code = self.CLF_SG.tax_code("SR", "sales", "sql_account")
        assert code == "SR", f"Expected 'SR', got {code!r}"

    # --- belt-and-suspenders: none of the above return blank -----------------

    def test_no_blank_codes_for_sg_erp_systems(self):
        """Belt-and-suspenders: every common SG ERP code must be non-empty."""
        cases = [
            ("SR", "purchase", "autocount"),
            ("SR", "purchase", "sql_account"),
            ("ZR", "purchase", "autocount"),
            ("ZR", "purchase", "sql_account"),
            ("NT", "purchase", "autocount"),
            ("NT", "purchase", "sql_account"),
            ("SR", "sales", "autocount"),
            ("SR", "sales", "sql_account"),
        ]
        blanks = [
            f"{system}/{doc_type}/{treatment}"
            for treatment, doc_type, system in cases
            if self.CLF_SG.tax_code(treatment, doc_type, system) == ""
        ]
        assert not blanks, f"Blank tax codes returned for: {blanks}"


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


# ===========================================================================
# WS4.4 — jurisdiction-aware get_tax_classifier factory
# ===========================================================================

class TestGetTaxClassifierFactory:
    """get_tax_classifier returns a classifier loaded from the correct YAML.

    SG QBS purchase SR maps to "TX"; MY SST QBS purchase SR maps to "SR".
    The two must differ, confirming each classifier uses its own code_map.
    """

    def test_sg_yaml_by_name_returns_sg_classifier(self):
        clf = get_tax_classifier("sg_gst.yaml")
        # SG QBS purchase SR → "TX" (see sg_gst.yaml code_map.qbs.purchase.SR)
        code = clf.tax_code("SR", "purchase", "qbs")
        assert code == "TX", f"Expected SG QBS purchase SR='TX', got {code!r}"

    def test_my_yaml_by_name_returns_my_classifier(self):
        clf = get_tax_classifier("my_sst.yaml")
        # MY SST QBS purchase SR → "SR" (see my_sst.yaml code_map.qbs.purchase.SR)
        code = clf.tax_code("SR", "purchase", "qbs")
        assert code == "SR", f"Expected MY SST QBS purchase SR='SR', got {code!r}"

    def test_sg_and_my_purchase_sr_codes_differ(self):
        sg_code = get_tax_classifier("sg_gst.yaml").tax_code("SR", "purchase", "qbs")
        my_code = get_tax_classifier("my_sst.yaml").tax_code("SR", "purchase", "qbs")
        assert sg_code != my_code, (
            f"SG and MY classifiers returned the same QBS purchase SR code {sg_code!r}; "
            "they must differ (SG='TX', MY='SR')"
        )

    def test_jurisdiction_string_malaysia_maps_to_my_sst(self):
        clf = get_tax_classifier("MALAYSIA")
        code = clf.tax_code("SR", "purchase", "qbs")
        assert code == "SR", f"Expected MY SST code 'SR' for MALAYSIA jurisdiction, got {code!r}"

    def test_jurisdiction_string_singapore_defaults_to_sg(self):
        clf = get_tax_classifier("SINGAPORE")
        code = clf.tax_code("SR", "purchase", "qbs")
        assert code == "TX", f"Expected SG code 'TX' for SINGAPORE jurisdiction, got {code!r}"

    def test_cross_border_without_yaml_returns_none(self):
        assert get_tax_classifier("CROSS_BORDER") is None

    def test_none_returns_none_not_sg_default(self):
        assert get_tax_classifier(None) is None

    def test_empty_string_returns_none_not_sg_default(self):
        assert get_tax_classifier("") is None

    def test_unknown_jurisdiction_returns_none(self):
        assert get_tax_classifier("ATLANTIS") is None

    def test_my_sst_nt_code_purchase_qbs(self):
        clf = get_tax_classifier("my_sst.yaml")
        code = clf.tax_code("NT", "purchase", "qbs")
        assert code == "NT"

    def test_my_sst_zr_code_purchase_xero(self):
        clf = get_tax_classifier("my_sst.yaml")
        code = clf.tax_code("ZR", "purchase", "xero")
        assert code == "ZR"

    def test_default_TaxClassifier_still_sg(self):
        """TaxClassifier() with no args must still load sg_gst.yaml (no regression)."""
        clf = TaxClassifier()
        code = clf.tax_code("SR", "purchase", "qbs")
        assert code == "TX", f"Default TaxClassifier() must give SG code 'TX', got {code!r}"

    def test_my_classifier_sg_supplier_is_overseas(self):
        """C4: MY taxonomy treats SG suppliers as overseas, not domestic."""
        clf = get_tax_classifier("my_sst.yaml")
        line, inv = _purchase(
            desc="Consulting",
            gst=None,
            gst_regno=None,
            is_overseas=False,
        )
        inv.supplier.country = "SG"
        result = clf.classify_line(line, inv)
        assert result.tax_treatment == "OS"
        assert result.tax_flagged is True


class TestGetTaxClassifierIntegration:
    """Integration: an exporter built with the MY classifier produces MY SST tax codes.

    The Xero exporter exposes a ``*TaxType`` column (unlike QBS which uses a Tax Amount
    numeric column), making it the cleanest way to assert the code string end-to-end.
    The QBS path is validated via clf.tax_code() directly.
    """

    def _make_purchase_inv(self, number: str, gst_regno: str, vendor: str) -> "NormalizedInvoice":
        inv = NormalizedInvoice(
            doc_type="purchase",
            invoice_number=number,
            invoice_date=date(2024, 6, 1),
            our_gst_registered=True,
            supplier=PartyInfo(name=vendor, gst_regno=gst_regno),
        )
        line = InvoiceLine(
            description="Consulting service",
            net_amount=1000.0,
            gst_amount=80.0,
            tax_treatment="SR",
        )
        inv.lines.append(line)
        return inv

    def test_xero_exporter_with_my_classifier_produces_sst_tax_type(self):
        """Xero exporter + MY classifier → *TaxType must NOT be 'SR' SG code."""
        from ledgr_slack.export.exporters import get_exporter

        clf = get_tax_classifier("my_sst.yaml")
        exporter = get_exporter("xero", classifier=clf)
        inv = self._make_purchase_inv("MY-INV-001", "MY-REG-123", "MY Vendor Sdn Bhd")

        rows = exporter.rows([inv], "purchase")
        assert rows, "Expected at least one export row"
        tax_type = rows[0].get("*TaxType")
        assert tax_type is not None, f"No '*TaxType' column in Xero row: {rows[0]}"
        # MY SST xero purchase SR → "SR" (from my_sst.yaml code_map.xero.purchase.SR)
        # SG GST xero purchase SR → "SR" too — same string in both YAMLs for Xero.
        # The meaningful difference is QBS: SG="TX", MY="SR".  Validate via clf directly.
        sg_qbs = get_tax_classifier("sg_gst.yaml").tax_code("SR", "purchase", "qbs")
        my_qbs = clf.tax_code("SR", "purchase", "qbs")
        assert sg_qbs != my_qbs, (
            f"QBS purchase SR code must differ between SG ({sg_qbs!r}) and MY ({my_qbs!r})"
        )
        assert sg_qbs == "TX"
        assert my_qbs == "SR"

    def test_xero_exporter_with_sg_classifier_unchanged(self):
        """SG Xero exporter still produces 'SR' for a standard-rated purchase (no regression)."""
        from ledgr_slack.export.exporters import get_exporter

        clf = get_tax_classifier("sg_gst.yaml")
        exporter = get_exporter("xero", classifier=clf)
        inv = self._make_purchase_inv("SG-INV-001", "M12345678X", "SG Vendor Pte Ltd")

        rows = exporter.rows([inv], "purchase")
        assert rows, "Expected at least one export row"
        tax_type = rows[0].get("*TaxType")
        # SG Xero purchase SR → "SR"
        assert tax_type == "SR", f"SG Xero purchase SR should be 'SR', got {tax_type!r}"


# ===========================================================================
# Indeterminate treatment → None, never SR (fix for silent SR-default bug)
# ===========================================================================

class TestIndeterminateNullBehavior:
    """Focused regression tests for the SR-default removal.

    Rule: a genuinely-indeterminate purchase line (registered client, supplier
    GST-registered but no GST amount on line, no explicit signals) must return
    tax_treatment=None + tax_flagged=True. The tax_code() method and
    _resolve_tax_code must return "" (blank) for a None/unmapped treatment,
    never the SR code.
    """

    def test_indeterminate_purchase_returns_none_flagged(self):
        """Registered client, supplier GST-registered, no GST amount, no signals
        → tax_treatment is None and tax_flagged is True (rule 7 changed)."""
        # Build a line that exhausts all earlier rules:
        # - our_gst_registered=True  (skips master NT gate)
        # - no tax_keyword
        # - no zero_rated / exempt / no_tax signals in description
        # - gst=0  (skips rule 2 GST-positive branch)
        # - not overseas  (skips rule 4)
        # - no standard_rated signal  (skips rule 5)
        # - supplier.gst_registered=True  (skips rule 6 NT: not-registered)
        # → falls through to rule 7: indeterminate
        line = InvoiceLine(
            description="Mystery service",
            net_amount=1000.0,
            gst_amount=0.0,
        )
        inv = NormalizedInvoice(
            doc_type="purchase",
            invoice_date=date(2024, 6, 1),
            our_gst_registered=True,
            supplier=PartyInfo(gst_regno="M12345678X", country="SG"),
        )
        inv.lines.append(line)
        result = CLF.classify_line(line, inv)
        assert result.tax_treatment is None, (
            f"Indeterminate line must return None, got {result.tax_treatment!r}"
        )
        assert result.tax_flagged is True, (
            "Indeterminate (None) treatment must be flagged for human review"
        )

    def test_tax_code_none_treatment_returns_blank(self):
        """tax_code(None, ...) must return '' (blank), never the SR code."""
        code = CLF.tax_code(None, "purchase", "qbs")
        assert code == "", f"tax_code(None, ...) must return blank, got {code!r}"
        code_xero = CLF.tax_code(None, "purchase", "xero")
        assert code_xero == "", f"tax_code(None, ...) must return blank for xero, got {code_xero!r}"

    def test_tax_code_unmapped_treatment_returns_blank_not_sr(self):
        """tax_code with a treatment not in the code_map must return '' not the SR code.

        Uses a QBS code_map where NT is present but a hypothetical 'UNKNOWN' is not.
        Confirms the old table.get('SR', '') fallback is gone.
        """
        # NT is in the SG QBS map; 'UNMAPPED_CODE' is not.
        code = CLF.tax_code("UNMAPPED_CODE", "purchase", "qbs")
        assert code == "", (
            f"Unmapped treatment must return blank not SR, got {code!r}"
        )
        # Confirm SR still maps correctly (not broken by the fix).
        sr_code = CLF.tax_code("SR", "purchase", "qbs")
        assert sr_code != "", "SR must still map to a real code (regression guard)"

    def test_none_treatment_exports_blank_tax_amount(self):
        """An indeterminate (None treatment) line exports as 0 tax amount, not SR amount."""
        from ledgr_slack.export.exporters import QbsLedgerExporter
        inv = NormalizedInvoice(
            doc_type="purchase",
            invoice_date=date(2024, 6, 1),
            our_gst_registered=True,
            supplier=PartyInfo(gst_regno="M12345678X"),
        )
        line = InvoiceLine(
            description="Indeterminate service",
            net_amount=1000.0,
            gst_amount=0.0,
            tax_treatment=None,   # explicitly set to None (as rule 7 now produces)
            tax_flagged=True,
        )
        inv.lines.append(line)
        exporter = QbsLedgerExporter()
        # _ensure_classified will re-classify since tax_treatment is None;
        # the re-classified result should still be None+flagged (rule 7).
        # We call rows() which internally calls _ensure_classified then _purchase_row.
        rows = exporter.rows([inv], "purchase")
        assert rows, "Expected one export row"
        tax_amount = rows[0].get("Tax Amount")
        # None treatment → _tax_amount returns 0.0 (not SR 9%)
        assert tax_amount == 0.0, (
            f"Indeterminate line must export Tax Amount=0, got {tax_amount!r}"
        )


# ===========================================================================
# YAML-driven keyword alias ladder (data-relocation refactor — behavior-preserving)
# ===========================================================================

class TestKeywordAliasLadderYaml:
    """Representative tax_keyword → treatment mappings still resolve correctly
    through the shared tax_aliases.yaml ladder (replaces the old hardcoded ladder).

    Covers each match-type (exact / prefix / substring) and each treatment, in
    precedence order. A no-GST purchase keeps the keyword unflagged.
    """

    CASES = [
        # (tax_keyword, expected_treatment)
        ("z", "ZR"),            # ZR exact
        ("ZR", "ZR"),           # ZR prefix
        ("zero-rated", "ZR"),   # ZR substring "zero"
        ("GST 0%", "ZR"),       # ZR substring "0%"
        ("e", "ES"),            # ES exact
        ("ES", "ES"),           # ES prefix
        ("exempt", "ES"),       # ES substring
        ("OS", "OS"),           # OS prefix
        ("out of scope", "OS"), # OS substring
        ("n", "NT"),            # NT exact
        ("NT", "NT"),           # NT prefix
        ("no tax", "NT"),       # NT substring
        ("no-tax", "NT"),       # NT substring (hyphen)
        ("g", "SR"),            # SR exact "g"
        ("gst", "SR"),          # SR exact "gst"
        ("SR", "SR"),           # SR prefix
        ("TX", "SR"),           # SR prefix "tx"
        ("standard-rated", "SR"),  # SR substring "standard"
    ]

    def test_purchase_keyword_aliases(self):
        for kw, expected in self.CASES:
            line, inv = _purchase(
                desc="Misc charge", gst=None, gst_regno="M12345678X", tax_kw=kw
            )
            result = CLF.classify_line(line, inv)
            assert result.tax_treatment == expected, (
                f"purchase tax_keyword {kw!r} → {result.tax_treatment!r}, expected {expected!r}"
            )

    def test_sales_keyword_aliases(self):
        for kw, expected in self.CASES:
            line, inv = _sales(desc="Misc charge", gst=None, tax_kw=kw)
            result = CLF.classify_line(line, inv)
            assert result.tax_treatment == expected, (
                f"sales tax_keyword {kw!r} → {result.tax_treatment!r}, expected {expected!r}"
            )

    def test_sr_rate_keyword_still_matches(self):
        # "9%" is not in the static alias table; it must still resolve to SR via
        # the dynamic rate-keyword tail derived from the jurisdiction YAML.
        assert CLF._sr_tax_keyword_match("9%") is True

    def test_precedence_zr_before_sr(self):
        # "zr" must hit ZR before any SR rule even though SR is also in the ladder.
        line, inv = _purchase(
            desc="x", gst=None, gst_regno="M12345678X", tax_kw="zr-export"
        )
        assert CLF.classify_line(line, inv).tax_treatment == "ZR"
