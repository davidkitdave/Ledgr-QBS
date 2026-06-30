"""Tests for the multi-country jurisdiction router (ledgr_slack.jurisdiction)."""

from __future__ import annotations



from ledgr_slack.jurisdiction import (
    CROSS_BORDER_KEY,
    FLAG_FOR_HUMAN_KEY,
    JURISDICTION_AMBIGUOUS,
    JURISDICTION_CROSS_BORDER,
    REGION_MALAYSIA,
    REGION_SINGAPORE,
    TAX_JURISDICTION_KEY,
    TAX_SYSTEM_GST,
    TAX_SYSTEM_OUT_OF_SCOPE,
    TAX_SYSTEM_SST,
    registration_threshold_for_region,
    resolve_jurisdiction,
    resolution_from_state,
    supported_regions,
    write_to_state,
)
from ledgr_slack.export.models import PartyInfo


class TestResolveJurisdiction:
    def test_singapore_profile(self):
        res = resolve_jurisdiction({"region": "SINGAPORE", "base_currency": "SGD"})
        assert res.jurisdiction.code == REGION_SINGAPORE
        assert res.jurisdiction.tax_system == TAX_SYSTEM_GST

    def test_malaysia_profile(self):
        res = resolve_jurisdiction({"region": "MALAYSIA", "base_currency": "MYR"})
        assert res.jurisdiction.code == REGION_MALAYSIA
        assert res.jurisdiction.tax_system == TAX_SYSTEM_SST
        assert res.jurisdiction.reference_yaml == "my_sst.yaml"

    def test_cross_border_sg_client_my_supplier(self):
        res = resolve_jurisdiction({
            "region": "SINGAPORE",
            "base_currency": "SGD",
            "supplier_country": "MY",
        })
        assert res.jurisdiction.code == JURISDICTION_CROSS_BORDER
        assert res.jurisdiction.cross_border is True

    def test_ambiguous_when_region_missing(self):
        res = resolve_jurisdiction({})
        assert res.jurisdiction.code == JURISDICTION_AMBIGUOUS
        assert res.jurisdiction.flag_for_human is True


class TestResolveJurisdictionCharacterization:
    def test_supported_regions_includes_sg_and_my(self):
        regions = supported_regions()
        assert REGION_SINGAPORE in regions
        assert REGION_MALAYSIA in regions

    def test_registration_threshold_sg(self):
        amount, currency, label = registration_threshold_for_region(REGION_SINGAPORE)
        assert amount > 0
        assert currency
        assert label

    def test_write_and_read_roundtrip(self):
        state: dict = {"region": "SINGAPORE", "base_currency": "SGD"}
        res = resolve_jurisdiction(state)
        write_to_state(state, res)
        rebuilt = resolution_from_state(state)
        assert rebuilt.jurisdiction.code == REGION_SINGAPORE
        assert state[TAX_JURISDICTION_KEY] == REGION_SINGAPORE


class TestPartyIsOverseasFor:
    def test_sg_supplier_not_overseas_for_sg_client(self):
        from ledgr_slack.export.models import PartyInfo

        party = PartyInfo(name="Local", country="SG")
        assert party.is_overseas_for(home_country="SG") is False

    def test_my_supplier_overseas_for_sg_client(self):
        party = PartyInfo(name="Foreign", country="MY")
        assert party.is_overseas_for(home_country="SG") is True


class TestCrossBorderAutoBook:
    def test_resolve_my_cross_border_auto_book(self):
        res = resolve_jurisdiction({
            "region": "MALAYSIA",
            "base_currency": "MYR",
            "supplier_country": "SG",
        })
        assert res.jurisdiction.code == JURISDICTION_CROSS_BORDER
        assert res.jurisdiction.cross_border is True
        assert res.jurisdiction.flag_for_human is False
        assert res.jurisdiction.tax_system == TAX_SYSTEM_OUT_OF_SCOPE

    def test_roundtrip_flag_preserved_false(self):
        state: dict = {
            "region": "MALAYSIA",
            "base_currency": "MYR",
            "supplier_country": "SG",
        }
        res = resolve_jurisdiction(state)
        write_to_state(state, res)
        assert state[FLAG_FOR_HUMAN_KEY] is False
        assert state[CROSS_BORDER_KEY] is True
        rebuilt = resolution_from_state(state)
        assert rebuilt.jurisdiction.flag_for_human is False
        assert rebuilt.jurisdiction.cross_border is True

    def test_sg_domestic_unchanged(self):
        res = resolve_jurisdiction({
            "region": "SINGAPORE",
            "base_currency": "SGD",
            "supplier_country": "SG",
        })
        assert res.jurisdiction.code == REGION_SINGAPORE
        assert res.jurisdiction.tax_system == TAX_SYSTEM_GST
        assert res.jurisdiction.flag_for_human is False

    def test_ambiguous_no_region_still_flags(self):
        res = resolve_jurisdiction({})
        assert res.jurisdiction.code == JURISDICTION_AMBIGUOUS
        assert res.jurisdiction.flag_for_human is True
