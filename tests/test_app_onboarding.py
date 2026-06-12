"""Tests for app/onboarding.py — pure logic, no Slack calls."""

from __future__ import annotations

import pytest

from app.onboarding import ProfileInput, parse_modal_state, profile_doc


def _make_view_state(
    client_name: str = "Acme Pte Ltd",
    fye_month: str = "3",
    accounting_software: str = "Xero",
    gst_value: str = "yes",
) -> dict:
    """Build a synthetic Slack view dict matching the onboarding modal block/action ids."""
    return {
        "state": {
            "values": {
                "client_name": {
                    "val": {"type": "plain_text_input", "value": client_name}
                },
                "fye_month": {
                    "val": {
                        "type": "static_select",
                        "selected_option": {
                            "text": {"type": "plain_text", "text": "March"},
                            "value": fye_month,
                        },
                    }
                },
                "accounting_software": {
                    "val": {
                        "type": "static_select",
                        "selected_option": {
                            "text": {"type": "plain_text", "text": accounting_software},
                            "value": accounting_software,
                        },
                    }
                },
                "gst_registered": {
                    "val": {
                        "type": "radio_buttons",
                        "selected_option": {
                            "text": {"type": "plain_text", "text": "Yes" if gst_value == "yes" else "No"},
                            "value": gst_value,
                        },
                    }
                },
            }
        }
    }


class TestParseModalState:

    def test_happy_path_returns_profile_input(self):
        view = _make_view_state()
        inp = parse_modal_state(view)
        assert isinstance(inp, ProfileInput)

    def test_client_name_parsed(self):
        inp = parse_modal_state(_make_view_state(client_name="Test Corp"))
        assert inp.client_name == "Test Corp"

    def test_fye_month_is_int(self):
        inp = parse_modal_state(_make_view_state(fye_month="3"))
        assert inp.fye_month == 3
        assert isinstance(inp.fye_month, int)

    def test_fye_month_december(self):
        inp = parse_modal_state(_make_view_state(fye_month="12"))
        assert inp.fye_month == 12

    def test_accounting_software_parsed(self):
        inp = parse_modal_state(_make_view_state(accounting_software="QBS Ledger"))
        assert inp.accounting_software == "QBS Ledger"

    def test_gst_registered_yes_is_true(self):
        inp = parse_modal_state(_make_view_state(gst_value="yes"))
        assert inp.gst_registered is True

    def test_gst_registered_no_is_false(self):
        inp = parse_modal_state(_make_view_state(gst_value="no"))
        assert inp.gst_registered is False

    def test_missing_state_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_modal_state({})

    def test_missing_client_name_block_raises_value_error(self):
        view = _make_view_state()
        del view["state"]["values"]["client_name"]
        with pytest.raises(ValueError, match="client_name"):
            parse_modal_state(view)

    def test_empty_client_name_raises_value_error(self):
        view = _make_view_state(client_name="  ")
        with pytest.raises(ValueError, match="client_name"):
            parse_modal_state(view)

    def test_missing_fye_month_block_raises_value_error(self):
        view = _make_view_state()
        del view["state"]["values"]["fye_month"]
        with pytest.raises(ValueError, match="fye_month"):
            parse_modal_state(view)

    def test_missing_accounting_software_block_raises_value_error(self):
        view = _make_view_state()
        del view["state"]["values"]["accounting_software"]
        with pytest.raises(ValueError, match="accounting_software"):
            parse_modal_state(view)

    def test_missing_gst_registered_block_raises_value_error(self):
        view = _make_view_state()
        del view["state"]["values"]["gst_registered"]
        with pytest.raises(ValueError, match="gst_registered"):
            parse_modal_state(view)


class TestProfileDoc:

    def _inp(self, **kwargs) -> ProfileInput:
        defaults = dict(
            client_name="Acme Pte Ltd",
            fye_month=3,
            accounting_software="Xero",
            gst_registered=True,
        )
        defaults.update(kwargs)
        return ProfileInput(**defaults)

    def test_returns_dict(self):
        doc = profile_doc(
            self._inp(),
            channel_id="C123",
            team_id="T456",
            client_id="client-abc",
        )
        assert isinstance(doc, dict)

    def test_client_id(self):
        doc = profile_doc(self._inp(), channel_id="C", team_id="T", client_id="cid-1")
        assert doc["client_id"] == "cid-1"

    def test_channel_id(self):
        doc = profile_doc(self._inp(), channel_id="C-CHAN", team_id="T", client_id="x")
        assert doc["channel_id"] == "C-CHAN"

    def test_slack_team_id(self):
        doc = profile_doc(self._inp(), channel_id="C", team_id="T-TEAM", client_id="x")
        assert doc["slack_team_id"] == "T-TEAM"

    def test_client_name(self):
        doc = profile_doc(self._inp(client_name="Newco"), channel_id="C", team_id="T", client_id="x")
        assert doc["client_name"] == "Newco"

    def test_fye_month_is_int(self):
        doc = profile_doc(self._inp(fye_month=12), channel_id="C", team_id="T", client_id="x")
        assert doc["fye_month"] == 12
        assert isinstance(doc["fye_month"], int)

    def test_accounting_software(self):
        doc = profile_doc(self._inp(accounting_software="QBS Ledger"), channel_id="C", team_id="T", client_id="x")
        assert doc["accounting_software"] == "QBS Ledger"

    def test_gst_registered_is_bool(self):
        doc = profile_doc(self._inp(gst_registered=True), channel_id="C", team_id="T", client_id="x")
        assert doc["gst_registered"] is True
        assert isinstance(doc["gst_registered"], bool)

    def test_gst_registered_false(self):
        doc = profile_doc(self._inp(gst_registered=False), channel_id="C", team_id="T", client_id="x")
        assert doc["gst_registered"] is False

    def test_region_default_singapore(self):
        doc = profile_doc(self._inp(), channel_id="C", team_id="T", client_id="x")
        assert doc["region"] == "SINGAPORE"

    def test_base_currency_default_sgd(self):
        doc = profile_doc(self._inp(), channel_id="C", team_id="T", client_id="x")
        assert doc["base_currency"] == "SGD"

    def test_status_pending_coa(self):
        doc = profile_doc(self._inp(), channel_id="C", team_id="T", client_id="x")
        assert doc["status"] == "pending_coa"

    def test_category_mapping_empty_dict(self):
        doc = profile_doc(self._inp(), channel_id="C", team_id="T", client_id="x")
        assert doc["category_mapping"] == {}

    def test_full_spec_shape(self):
        """All required spec §1 keys are present."""
        doc = profile_doc(self._inp(), channel_id="C123", team_id="T456", client_id="cid")
        required_keys = {
            "client_id", "channel_id", "slack_team_id", "client_name",
            "fye_month", "accounting_software", "gst_registered",
            "region", "base_currency", "status", "category_mapping",
        }
        assert required_keys.issubset(doc.keys())
