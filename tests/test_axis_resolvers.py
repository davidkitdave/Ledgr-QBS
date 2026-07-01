"""WS-4.2 — one resolver per axis: blank+flag, no silent SG/SGD/QBS defaults."""

from __future__ import annotations

from ledgr_slack.jurisdiction import REGION_MALAYSIA, REGION_SINGAPORE
from ledgr_slack.export.axis_resolvers import resolve_currency, resolve_software
from ledgr_slack.client_context import _profile_region_and_currency


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


class TestLegacyProfileNoSilentSingapore:
    def test_legacy_profile_does_not_force_singapore(self):
        region, currency = _profile_region_and_currency({"legacy_profile": True})
        assert region == ""
        assert currency == ""
