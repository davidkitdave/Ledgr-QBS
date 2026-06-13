"""Tests for app/slack_app.py — Bolt wiring and FastAPI app.

All tests use InMemoryClientStore and fake ack/client objects.
No live Slack token or Firestore call is made.
"""

from __future__ import annotations

import io
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app import slack_app
from app.slack_app import (
    FileTooLargeError,
    build_app,
    fastapi_app,
    handle_file_share,
    handle_onboarding_submit,
    handle_setup_open,
    slack_download_file,
)
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
        # Task 3: 2 messages — profile summary first, then COA prompt.
        assert len(client.posted_messages) == 2

    def test_chat_post_message_channel(self):
        _, _, client = self._run()
        msg = client.posted_messages[0]
        assert msg["channel"] == "C-TEST-1"

    def test_chat_post_message_has_coa_prompt_blocks(self):
        from app.blocks import coa_prompt_blocks
        _, _, client = self._run()
        # COA prompt is the second message; first is the profile summary.
        msg = client.posted_messages[1]
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

    def test_onboarding_posts_profile_summary_then_coa_prompt(self):
        body = _submit_body(
            client_name="Auditair International Pte. Ltd.",
            fye_month="10", accounting_software="Xero", gst_value="no",
            channel_id="C1",
        )
        ack = FakeAck()
        client = FakeClient()
        store = InMemoryClientStore()
        handle_onboarding_submit(body, ack, client, store, _fixed_id_factory)

        joined = " ".join(
            blk.get("text", {}).get("text", "")
            for msg in client.posted_messages
            for blk in msg.get("blocks", [])
            if isinstance(blk.get("text"), dict)
        )
        assert "Client registered" in joined
        assert "Xero" in joined
        assert "Profile saved" in joined  # the COA prompt still follows


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

    def test_healthz_returns_ok_when_configured(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-tok")
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "sig")
        from fastapi.testclient import TestClient
        store = InMemoryClientStore()
        api = fastapi_app(store=store)
        tc = TestClient(api)
        resp = tc.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_healthz_returns_503_when_env_missing(self, monkeypatch):
        # Item 9: fail loud — missing Slack HTTP env vars surface as 503 + list.
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
        from fastapi.testclient import TestClient
        store = InMemoryClientStore()
        api = fastapi_app(store=store)
        tc = TestClient(api)
        resp = tc.get("/healthz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["ok"] is False
        assert set(body["missing"]) == {"SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"}


# --------------------------------------------------------------------------- #
# slack_download_file hardening (items 3, 4, 5)
# --------------------------------------------------------------------------- #

class _DLClient:
    """Fake WebClient whose files_info returns a controllable file_meta."""

    def __init__(self, name="invoice.pdf", url="https://files.slack.com/x", size=None):
        self._meta = {"url_private_download": url, "name": name}
        if size is not None:
            self._meta["size"] = size
        self.token = "xoxb-fake"

    def files_info(self, file):
        return {"file": dict(self._meta)}


def _fake_opener(payload: bytes = b"PDFDATA"):
    """Patch _SLACK_OPENER.open to return a streamable fake response."""
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()
            return False

    return lambda req: _Resp(payload)


class TestSlackDownloadFileHardening:

    def test_path_traversal_lands_inside_dest(self, tmp_path):
        client = _DLClient(name="../../evil.pdf")
        with patch.object(slack_app._SLACK_OPENER, "open", _fake_opener()):
            dest = slack_download_file(client, "F1", str(tmp_path))
        real = os.path.realpath(dest)
        assert real.startswith(os.path.realpath(str(tmp_path)) + os.sep)
        assert os.path.dirname(real) == os.path.realpath(str(tmp_path))
        assert ".." not in os.path.basename(dest)

    def test_absolute_path_name_lands_inside_dest(self, tmp_path):
        client = _DLClient(name="/etc/passwd")
        with patch.object(slack_app._SLACK_OPENER, "open", _fake_opener()):
            dest = slack_download_file(client, "F2", str(tmp_path))
        real = os.path.realpath(dest)
        assert real.startswith(os.path.realpath(str(tmp_path)) + os.sep)
        assert os.path.basename(dest) == "F2_passwd"

    def test_same_name_files_get_distinct_paths(self, tmp_path):
        c1 = _DLClient(name="invoice.pdf")
        c2 = _DLClient(name="invoice.pdf")
        with patch.object(slack_app._SLACK_OPENER, "open", _fake_opener()):
            d1 = slack_download_file(c1, "FAAA", str(tmp_path))
            d2 = slack_download_file(c2, "FBBB", str(tmp_path))
        assert d1 != d2
        assert os.path.basename(d1) == "FAAA_invoice.pdf"
        assert os.path.basename(d2) == "FBBB_invoice.pdf"

    def test_non_slack_url_rejected(self, tmp_path):
        client = _DLClient(url="https://evil.example.com/x")
        with patch.object(slack_app._SLACK_OPENER, "open", _fake_opener()):
            with pytest.raises(ValueError):
                slack_download_file(client, "F3", str(tmp_path))

    def test_oversize_file_rejected_before_fetch(self, tmp_path):
        client = _DLClient(size=slack_app.MAX_FILE_BYTES + 1)
        opened: list = []

        def _tracking_open(req):
            opened.append(req)
            return _fake_opener()(req)

        with patch.object(slack_app._SLACK_OPENER, "open", _tracking_open):
            with pytest.raises(FileTooLargeError):
                slack_download_file(client, "F4", str(tmp_path))
        # Size guard fires before any network fetch.
        assert opened == []

    def test_under_cap_file_downloads(self, tmp_path):
        client = _DLClient(size=10)
        with patch.object(slack_app._SLACK_OPENER, "open", _fake_opener(b"OK")):
            dest = slack_download_file(client, "F5", str(tmp_path))
        with open(dest, "rb") as fh:
            assert fh.read() == b"OK"


# --------------------------------------------------------------------------- #
# handle_file_share batch cap (item 5)
# --------------------------------------------------------------------------- #

class TestHandleFileShareBatchCap:

    def test_over_cap_batch_trimmed_with_notice(self):
        posted: list = []
        share_calls: list = []
        client = MagicMock()

        def _post(**kw):
            posted.append(kw)

        client.chat_postMessage.side_effect = _post

        store = InMemoryClientStore()
        store.save_profile({"client_id": "cli-cap", "channel_id": "C-CAP",
                            "fye_month": 12, "status": "active"})

        n = slack_app.MAX_FILES_PER_BATCH + 5
        files = [{"id": f"F{i}", "filetype": "pdf", "name": f"d{i}.pdf"} for i in range(n)]

        with patch.object(slack_app, "_seen_events", slack_app._SeenEvents()), \
             patch.object(slack_app, "run_share",
                          side_effect=lambda **kw: share_calls.append(kw)), \
             patch.object(slack_app, "slack_download_file", return_value="/tmp/x.pdf"), \
             patch.object(slack_app._executor, "submit",
                          side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
            event = {"channel": "C-CAP", "files": files}
            handle_file_share(event, client=client, store=store)

        # User was told some files were skipped.
        assert any("skipped" in (m.get("text") or "") for m in posted)
        # Only the cap number of files reached the worker.
        assert len(share_calls) == 1
        assert len(share_calls[0]["file_ids"]) == slack_app.MAX_FILES_PER_BATCH
