"""WS-4.2 — one resolver per axis: blank+flag, no silent SG/SGD/QBS defaults."""

from __future__ import annotations

import pytest


from ledgr_slack.jurisdiction import REGION_MALAYSIA, REGION_SINGAPORE
from ledgr_slack.export.axis_resolvers import (
    resolve_currency,
    resolve_software,
    resolve_tax_classifier_reference,
)
from ledgr_slack.client_context import _profile_region_and_currency
from ledgr_slack.export.tax_classifier import get_tax_classifier


class TestResolveSoftware:
    def test_known_qbs_not_flagged(self):
        res = resolve_software("QBS Ledger")
        assert res.flagged is False
        assert res.value == "qbs"

    def test_unknown_software_flagged_blank(self):
        res = resolve_software("Wave")
        assert res.flagged is True
        assert res.value is None
        assert "unknown software" in res.reason

    def test_missing_software_flagged_blank(self):
        res = resolve_software(None)
        assert res.flagged is True
        assert res.value is None
        assert "not set" in res.reason


class TestResolveCurrency:
    def test_document_currency_wins(self):
        res = resolve_currency("myr", client_region=REGION_SINGAPORE, client_currency="SGD")
        assert res.flagged is False
        assert res.value == "MYR"

    def test_client_profile_currency_not_flagged(self):
        res = resolve_currency(None, client_region=REGION_MALAYSIA, client_currency="MYR")
        assert res.flagged is False
        assert res.value == "MYR"

    def test_registry_currency_from_region_not_flagged(self):
        res = resolve_currency(None, client_region=REGION_SINGAPORE)
        assert res.flagged is False
        assert res.value == "SGD"

    def test_no_document_no_profile_flags_blank(self):
        res = resolve_currency(None)
        assert res.flagged is True
        assert res.value == ""
        assert "no client profile" in res.reason.lower()


class TestResolveTaxClassifierReference:
    pytestmark = pytest.mark.legacy

    def test_none_reference_flagged_no_classifier(self):
        res = resolve_tax_classifier_reference(None)
        assert res.flagged is True
        assert res.value is None

    def test_ambiguous_jurisdiction_flagged(self):
        res = resolve_tax_classifier_reference("AMBIGUOUS")
        assert res.flagged is True
        assert res.value is None

    def test_singapore_yaml_loads(self):
        res = resolve_tax_classifier_reference("sg_gst.yaml")
        assert res.flagged is False
        assert res.value is not None
        assert res.value.tax_code("SR", "purchase", "qbs") == "TX"

    def test_get_tax_classifier_none_returns_none(self):
        assert get_tax_classifier(None) is None

    def test_get_tax_classifier_empty_returns_none(self):
        assert get_tax_classifier("") is None

    def test_get_tax_classifier_unknown_jurisdiction_returns_none(self):
        assert get_tax_classifier("ATLANTIS") is None


class TestLegacyProfileNoSilentSingapore:
    def test_legacy_profile_does_not_force_singapore(self):
        region, currency = _profile_region_and_currency({"legacy_profile": True})
        assert region == ""
        assert currency == ""


class TestSalesIndeterminateFlagged:
    pytestmark = pytest.mark.legacy

    def test_sales_indeterminate_local_no_gst_flags(self):
        from datetime import date

        from ledgr_slack.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
        from ledgr_slack.export.tax_classifier import TaxClassifier

        clf = TaxClassifier()
        line = InvoiceLine(description="Misc local service", net_amount=100.0, gst_amount=None)
        inv = NormalizedInvoice(
            doc_type="sales",
            invoice_date=date(2024, 6, 1),
            our_gst_registered=True,
            customer=PartyInfo(name="Local Co", country="SG"),
        )
        inv.lines.append(line)
        result = clf.classify_line(line, inv)
        assert result.tax_treatment == "SR"
        assert result.tax_flagged is True
        assert "review" in (result.tax_reason or "").lower()
