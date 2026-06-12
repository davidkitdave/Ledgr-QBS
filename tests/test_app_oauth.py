"""Tests for multi-workspace OAuth wiring in app/slack_app.py (plan task #5.2).

All tests are hermetic: Firestore stores use injected fake clients and no real
Slack/GCP call is made. The OAuth install flow is never actually completed.
"""

from __future__ import annotations

import json

import pytest
from slack_bolt import App
from slack_bolt.oauth.oauth_settings import OAuthSettings

from app.slack_app import (
    BOT_SCOPES,
    build_app,
    build_oauth_settings,
    fastapi_app,
)
from app.installation_store import (
    FirestoreInstallationStore,
    FirestoreOAuthStateStore,
)
from invoice_processing.export.client_context import InMemoryClientStore


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _FakeDB:
    """Minimal Firestore client stand-in (never used — no network in these tests)."""

    def collection(self, *_a, **_k):  # pragma: no cover - defensive
        raise AssertionError("Firestore must not be touched in OAuth wiring tests")


def _injected_stores():
    db = _FakeDB()
    return (
        FirestoreInstallationStore(client=db),
        FirestoreOAuthStateStore(client=db),
    )


def _set_oauth_env(monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret-xyz")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "sig-abc")
    monkeypatch.setenv("SLACK_BASE_URL", "https://ledgr.example.run.app")
    monkeypatch.setenv("SLACK_OAUTH_STATE_SECRET", "state-secret")


def _clear_oauth_env(monkeypatch):
    for var in (
        "SLACK_CLIENT_ID",
        "SLACK_CLIENT_SECRET",
        "SLACK_SIGNING_SECRET",
        "SLACK_BASE_URL",
        "SLACK_OAUTH_STATE_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# build_oauth_settings
# --------------------------------------------------------------------------- #

class TestBuildOAuthSettings:

    def test_returns_none_when_env_unset(self, monkeypatch):
        _clear_oauth_env(monkeypatch)
        inst, state = _injected_stores()
        assert build_oauth_settings(installation_store=inst, state_store=state) is None

    def test_returns_oauth_settings_when_env_set(self, monkeypatch):
        _set_oauth_env(monkeypatch)
        inst, state = _injected_stores()
        settings = build_oauth_settings(installation_store=inst, state_store=state)
        assert isinstance(settings, OAuthSettings)

    def test_oauth_settings_uses_bot_scopes(self, monkeypatch):
        _set_oauth_env(monkeypatch)
        inst, state = _injected_stores()
        settings = build_oauth_settings(installation_store=inst, state_store=state)
        assert settings.scopes == BOT_SCOPES

    def test_oauth_settings_redirect_uri_from_base_url(self, monkeypatch):
        _set_oauth_env(monkeypatch)
        inst, state = _injected_stores()
        settings = build_oauth_settings(installation_store=inst, state_store=state)
        assert settings.redirect_uri == (
            "https://ledgr.example.run.app/slack/oauth_redirect"
        )

    def test_oauth_settings_install_paths(self, monkeypatch):
        _set_oauth_env(monkeypatch)
        inst, state = _injected_stores()
        settings = build_oauth_settings(installation_store=inst, state_store=state)
        assert settings.install_path == "/slack/install"
        assert settings.redirect_uri_path == "/slack/oauth_redirect"

    def test_oauth_settings_uses_injected_stores(self, monkeypatch):
        _set_oauth_env(monkeypatch)
        inst, state = _injected_stores()
        settings = build_oauth_settings(installation_store=inst, state_store=state)
        assert settings.installation_store is inst
        assert settings.state_store is state


# --------------------------------------------------------------------------- #
# build_app in OAuth mode
# --------------------------------------------------------------------------- #

class TestBuildAppOAuth:

    def _oauth_settings(self):
        inst, state = _injected_stores()
        return OAuthSettings(
            client_id="111.222",
            client_secret="secret-xyz",
            scopes=BOT_SCOPES,
            installation_store=inst,
            state_store=state,
            install_path="/slack/install",
            redirect_uri_path="/slack/oauth_redirect",
        )

    def test_constructs_app_without_raising(self):
        store = InMemoryClientStore()
        app = build_app(
            store,
            signing_secret="sig-abc",
            oauth_settings=self._oauth_settings(),
        )
        assert isinstance(app, App)

    def test_oauth_app_wired_with_installation_store(self):
        store = InMemoryClientStore()
        oauth = self._oauth_settings()
        app = build_app(
            store,
            signing_secret="sig-abc",
            oauth_settings=oauth,
        )
        # In OAuth mode the per-team token is resolved by the installation store
        # (InstallationStoreAuthorize); the app carries the OAuth flow.
        assert app.oauth_flow is not None
        assert app.installation_store is oauth.installation_store

    def test_single_token_mode_still_works(self):
        store = InMemoryClientStore()
        app = build_app(store)  # no oauth_settings -> single-workspace mode
        assert isinstance(app, App)
        assert app.client.token == "xoxb-test"


# --------------------------------------------------------------------------- #
# fastapi_app in OAuth mode
# --------------------------------------------------------------------------- #

class TestFastapiAppOAuth:

    def _route_paths(self, api):
        return {r.path for r in api.routes}

    def test_oauth_routes_present(self, monkeypatch):
        _set_oauth_env(monkeypatch)
        store = InMemoryClientStore()
        api = fastapi_app(store=store)
        paths = self._route_paths(api)
        assert "/slack/install" in paths
        assert "/slack/oauth_redirect" in paths
        assert "/slack/events" in paths

    def test_healthz_ok_when_oauth_configured(self, monkeypatch):
        _set_oauth_env(monkeypatch)
        # Even with no SLACK_BOT_TOKEN, OAuth config alone keeps healthz green.
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        from fastapi.testclient import TestClient
        store = InMemoryClientStore()
        api = fastapi_app(store=store)
        tc = TestClient(api)
        resp = tc.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_single_workspace_has_no_oauth_routes(self, monkeypatch):
        _clear_oauth_env(monkeypatch)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-tok")
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "sig")
        store = InMemoryClientStore()
        api = fastapi_app(store=store)
        paths = self._route_paths(api)
        assert "/slack/install" not in paths
        assert "/slack/oauth_redirect" not in paths
        assert "/slack/events" in paths


# --------------------------------------------------------------------------- #
# slack/manifest-distributed.json
# --------------------------------------------------------------------------- #

class TestDistributedManifest:

    def _manifest(self):
        with open("slack/manifest-distributed.json") as fh:
            return json.load(fh)

    def test_valid_json(self):
        assert isinstance(self._manifest(), dict)

    def test_socket_mode_disabled(self):
        m = self._manifest()
        assert m["settings"]["socket_mode_enabled"] is False

    def test_has_oauth_redirect_urls(self):
        m = self._manifest()
        assert m["oauth_config"]["redirect_urls"] == [
            "https://YOUR_CLOUD_RUN_URL/slack/oauth_redirect"
        ]

    def test_scopes_match_bot_scopes(self):
        m = self._manifest()
        assert m["oauth_config"]["scopes"]["bot"] == BOT_SCOPES

    def test_has_four_bot_events(self):
        m = self._manifest()
        events = m["settings"]["event_subscriptions"]["bot_events"]
        assert set(events) == {
            "member_joined_channel",
            "message.channels",
            "message.groups",
            "file_shared",
        }

    def test_event_subscriptions_request_url(self):
        m = self._manifest()
        assert m["settings"]["event_subscriptions"]["request_url"] == (
            "https://YOUR_CLOUD_RUN_URL/slack/events"
        )

    def test_interactivity_enabled_with_request_url(self):
        m = self._manifest()
        interactivity = m["settings"]["interactivity"]
        assert interactivity["is_enabled"] is True
        assert interactivity["request_url"] == (
            "https://YOUR_CLOUD_RUN_URL/slack/events"
        )

    def test_ledgr_slash_command(self):
        m = self._manifest()
        cmds = m["features"]["slash_commands"]
        ledgr = [c for c in cmds if c["command"] == "/ledgr"]
        assert len(ledgr) == 1
        assert ledgr[0]["url"] == "https://YOUR_CLOUD_RUN_URL/slack/events"
