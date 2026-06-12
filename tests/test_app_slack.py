"""Tests for app/slack_app.py — Bolt wiring and FastAPI app.

All tests use InMemoryClientStore and fake ack/client objects.
No live Slack token or Firestore call is made.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.slack_app import build_app, fastapi_app, handle_onboarding_submit, handle_setup_open
from invoice_processing.export.client_context import InMemoryClientStore


# --------------------------------------------------------------------------- #
# Helpers / fakes
# --------------------------------------------------------------------------- #

class FakeAck:
    """Records whether ack() was called."""

    def __init__(self):
        self.called = False

    def __call__(self, *args, **kwargs):
        self.called = True


class FakeClient:
    """Records Slack API calls made by handlers."""

    def __init__(self, bot_user_id: str = "U-BOT"):
        self.posted_messages: list[dict] = []
        self.opened_views: list[dict] = []
        self._bot_user_id = bot_user_id

    def chat_postMessage(self, **kwargs):
        self.posted_messages.append(kwargs)
        return {"ok": True}

    def views_open(self, **kwargs):
        self.opened_views.append(kwargs)
        return {"ok": True}

    def auth_test(self):
        return {"user_id": self._bot_user_id}


def _view_state_values(
    client_name: str = "TestCo Pte Ltd",
    fye_month: str = "12",
    accounting_software: str = "QBS Ledger",
    gst_value: str = "yes",
) -> dict:
    return {
        "client_name": {"val": {"type": "plain_text_input", "value": client_name}},
        "fye_month": {
            "val": {
                "type": "static_select",
                "selected_option": {"text": {"type": "plain_text", "text": "December"}, "value": fye_month},
            }
        },
        "accounting_software": {
            "val": {
                "type": "static_select",
                "selected_option": {"text": {"type": "plain_text", "text": accounting_software}, "value": accounting_software},
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


def _submit_body(
    channel_id: str = "C-TEST-1",
    team_id: str = "T-TEAM-1",
    client_name: str = "TestCo Pte Ltd",
    fye_month: str = "12",
    accounting_software: str = "QBS Ledger",
    gst_value: str = "yes",
) -> dict:
    """Build a synthetic Slack view_submission body."""
    return {
        "team": {"id": team_id},
        "view": {
            "callback_id": "ledgr_onboarding",
            "private_metadata": channel_id,
            "state": {"values": _view_state_values(client_name, fye_month, accounting_software, gst_value)},
        },
    }


# --------------------------------------------------------------------------- #
# handle_setup_open
# --------------------------------------------------------------------------- #

class TestHandleSetupOpen:

    def test_acks(self):
        ack = FakeAck()
        client = FakeClient()
        body = {
            "trigger_id": "trig-1",
            "channel": {"id": "C-OPEN"},
        }
        handle_setup_open(body, ack, client)
        assert ack.called

    def test_opens_modal(self):
        ack = FakeAck()
        client = FakeClient()
        body = {"trigger_id": "trig-1", "channel": {"id": "C-OPEN"}}
        handle_setup_open(body, ack, client)
        assert len(client.opened_views) == 1

    def test_modal_private_metadata_is_channel_id(self):
        ack = FakeAck()
        client = FakeClient()
        body = {"trigger_id": "trig-1", "channel": {"id": "C-OPEN"}}
        handle_setup_open(body, ack, client)
        view = client.opened_views[0]["view"]
        assert view["private_metadata"] == "C-OPEN"

    def test_modal_callback_id_is_ledgr_onboarding(self):
        ack = FakeAck()
        client = FakeClient()
        body = {"trigger_id": "trig-1", "channel": {"id": "C-X"}}
        handle_setup_open(body, ack, client)
        view = client.opened_views[0]["view"]
        assert view["callback_id"] == "ledgr_onboarding"


# --------------------------------------------------------------------------- #
# handle_onboarding_submit
# --------------------------------------------------------------------------- #

FIXED_CLIENT_ID = "client-fixed-abc"


def _fixed_id_factory() -> str:
    return FIXED_CLIENT_ID


class TestHandleOnboardingSubmit:

    def _run(self, body: dict | None = None, store: InMemoryClientStore | None = None):
        store = store or InMemoryClientStore()
        ack = FakeAck()
        client = FakeClient()
        b = body or _submit_body()
        handle_onboarding_submit(b, ack, client, store, _fixed_id_factory)
        return store, ack, client

    def test_acks(self):
        _, ack, _ = self._run()
        assert ack.called

    def test_store_has_profile_for_channel(self):
        store, _, _ = self._run()
        ctx = store.get_by_channel("C-TEST-1")
        assert ctx is not None

    def test_profile_client_id(self):
        store, _, _ = self._run()
        ctx = store.get_by_channel("C-TEST-1")
        assert ctx.client_id == FIXED_CLIENT_ID

    def test_profile_client_name(self):
        store, _, _ = self._run()
        ctx = store.get_by_channel("C-TEST-1")
        assert ctx.client_name == "TestCo Pte Ltd"

    def test_profile_fye_month(self):
        store, _, _ = self._run()
        ctx = store.get_by_channel("C-TEST-1")
        assert ctx.fye_month == 12

    def test_profile_gst_registered_true(self):
        store, _, _ = self._run()
        ctx = store.get_by_channel("C-TEST-1")
        assert ctx.tax_registered is True

    def test_profile_gst_registered_false(self):
        body = _submit_body(gst_value="no")
        store, _, _ = self._run(body=body)
        ctx = store.get_by_channel("C-TEST-1")
        assert ctx.tax_registered is False

    def test_profile_status_pending_coa(self):
        store, _, _ = self._run()
        ctx = store.get_by_channel("C-TEST-1")
        assert ctx.status == "pending_coa"

    def test_profile_region_singapore(self):
        store, _, _ = self._run()
        ctx = store.get_by_channel("C-TEST-1")
        assert ctx.region == "SINGAPORE"

    def test_profile_base_currency_sgd(self):
        store, _, _ = self._run()
        ctx = store.get_by_channel("C-TEST-1")
        assert ctx.base_currency == "SGD"

    def test_chat_post_message_called(self):
        _, _, client = self._run()
        assert len(client.posted_messages) == 1

    def test_chat_post_message_channel(self):
        _, _, client = self._run()
        msg = client.posted_messages[0]
        assert msg["channel"] == "C-TEST-1"

    def test_chat_post_message_has_coa_prompt_blocks(self):
        from app.blocks import coa_prompt_blocks
        _, _, client = self._run()
        msg = client.posted_messages[0]
        assert msg.get("blocks") == coa_prompt_blocks()

    def test_different_channel_ids_are_isolated(self):
        store = InMemoryClientStore()
        body1 = _submit_body(channel_id="C-AAA", client_name="ClientA")
        body2 = _submit_body(channel_id="C-BBB", client_name="ClientB")
        # use distinct factories so IDs differ
        ack = FakeAck()
        client = FakeClient()
        handle_onboarding_submit(body1, ack, client, store, lambda: "id-aaa")
        handle_onboarding_submit(body2, FakeAck(), FakeClient(), store, lambda: "id-bbb")
        assert store.get_by_channel("C-AAA").client_name == "ClientA"
        assert store.get_by_channel("C-BBB").client_name == "ClientB"


# --------------------------------------------------------------------------- #
# build_app
# --------------------------------------------------------------------------- #

class TestBuildApp:

    def test_returns_bolt_app(self):
        from slack_bolt import App
        app = build_app(InMemoryClientStore())
        assert isinstance(app, App)

    def test_custom_id_factory(self):
        store = InMemoryClientStore()
        app = build_app(store, id_factory=lambda: "custom-id-xyz")
        assert app is not None  # wiring complete without error


# --------------------------------------------------------------------------- #
# fastapi_app + healthz
# --------------------------------------------------------------------------- #

class TestFastapiApp:

    def test_constructs_without_error(self):
        store = InMemoryClientStore()
        api = fastapi_app(store=store)
        assert api is not None

    def test_healthz_returns_ok(self):
        from fastapi.testclient import TestClient
        store = InMemoryClientStore()
        api = fastapi_app(store=store)
        tc = TestClient(api)
        resp = tc.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
