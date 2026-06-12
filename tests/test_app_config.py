"""Tests for app/config.py, app/main.py, slack/manifest.json, and app/socket_run.py."""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# app.config — Settings dataclass and helper functions
# ---------------------------------------------------------------------------

class TestGetSettings:
    def test_all_set(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
        monkeypatch.setenv("PROJECT_ID", "my-project")
        monkeypatch.setenv("LOCATION", "asia-southeast1")
        # avoid GOOGLE_CLOUD_PROJECT shadowing
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

        from app.config import get_settings
        s = get_settings()
        assert s.slack_bot_token == "xoxb-test"
        assert s.slack_signing_secret == "secret"
        assert s.slack_app_token == "xapp-test"
        assert s.gcp_project == "my-project"
        assert s.location == "asia-southeast1"

    def test_google_cloud_project_fallback(self, monkeypatch):
        monkeypatch.delenv("PROJECT_ID", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gcp-proj")
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        monkeypatch.delenv("LOCATION", raising=False)

        from app.config import get_settings
        s = get_settings()
        assert s.gcp_project == "gcp-proj"

    def test_all_missing(self, monkeypatch):
        for var in ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "SLACK_APP_TOKEN",
                    "PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "LOCATION"):
            monkeypatch.delenv(var, raising=False)

        from app.config import get_settings
        s = get_settings()
        assert s.slack_bot_token is None
        assert s.slack_signing_secret is None
        assert s.slack_app_token is None
        assert s.gcp_project is None
        assert s.location is None


class TestMissingSlackHttp:
    def test_nothing_missing(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-tok")
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "sig")

        from app.config import missing_slack_http
        assert missing_slack_http() == []

    def test_both_missing(self, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)

        from app.config import missing_slack_http
        result = missing_slack_http()
        assert "SLACK_BOT_TOKEN" in result
        assert "SLACK_SIGNING_SECRET" in result

    def test_only_token_missing(self, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "sig")

        from app.config import missing_slack_http
        result = missing_slack_http()
        assert result == ["SLACK_BOT_TOKEN"]

    def test_only_secret_missing(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-tok")
        monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)

        from app.config import missing_slack_http
        result = missing_slack_http()
        assert result == ["SLACK_SIGNING_SECRET"]


class TestMissingSlackSocket:
    def test_nothing_missing(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-tok")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-tok")

        from app.config import missing_slack_socket
        assert missing_slack_socket() == []

    def test_both_missing(self, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

        from app.config import missing_slack_socket
        result = missing_slack_socket()
        assert "SLACK_BOT_TOKEN" in result
        assert "SLACK_APP_TOKEN" in result

    def test_only_app_token_missing(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-tok")
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

        from app.config import missing_slack_socket
        assert missing_slack_socket() == ["SLACK_APP_TOKEN"]

    def test_signing_secret_not_required(self, monkeypatch):
        """Socket Mode does not require SLACK_SIGNING_SECRET."""
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-tok")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-tok")
        monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)

        from app.config import missing_slack_socket
        assert missing_slack_socket() == []


# ---------------------------------------------------------------------------
# app.main — import and /healthz
# ---------------------------------------------------------------------------

class TestAppMain:
    def test_import_produces_fastapi_instance(self):
        """Import app.main without network calls; check it's a FastAPI app."""
        from fastapi import FastAPI
        import app.main  # noqa: F401 — side-effect import under test
        assert isinstance(app.main.app, FastAPI)

    def test_healthz_returns_200_when_configured(self, monkeypatch):
        # healthz re-checks env at request time; with Slack HTTP vars set it is 200.
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-tok")
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "sig")
        from fastapi.testclient import TestClient
        import app.main
        client = TestClient(app.main.app)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_healthz_returns_503_when_unconfigured(self, monkeypatch):
        # Fail-LOUD: missing Slack HTTP vars → 503 with the missing list.
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
        from fastapi.testclient import TestClient
        import app.main
        client = TestClient(app.main.app)
        resp = client.get("/healthz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["ok"] is False
        assert "SLACK_BOT_TOKEN" in body["missing"]
        assert "SLACK_SIGNING_SECRET" in body["missing"]


# ---------------------------------------------------------------------------
# slack/manifest.json — structure validation
# ---------------------------------------------------------------------------

class TestManifest:
    @pytest.fixture(scope="class")
    def manifest(self):
        path = PROJECT_ROOT / "slack" / "manifest.json"
        return json.loads(path.read_text())

    def test_valid_json(self, manifest):
        assert isinstance(manifest, dict)

    def test_required_bot_scopes(self, manifest):
        scopes = manifest["oauth_config"]["scopes"]["bot"]
        for required in ("chat:write", "files:read", "files:write", "commands"):
            assert required in scopes, f"Missing bot scope: {required}"

    def test_all_specified_bot_scopes(self, manifest):
        scopes = manifest["oauth_config"]["scopes"]["bot"]
        expected = [
            "chat:write", "files:read", "files:write",
            "channels:history", "groups:history", "im:history",
            "channels:read", "groups:read", "commands",
            "app_mentions:read", "users:read",
        ]
        for scope in expected:
            assert scope in scopes, f"Missing scope: {scope}"

    def test_bot_events(self, manifest):
        events = manifest["settings"]["event_subscriptions"]["bot_events"]
        for required in (
            "member_joined_channel",
            "message.channels",
            "message.groups",
            "file_shared",
        ):
            assert required in events, f"Missing bot event: {required}"

    def test_slash_command(self, manifest):
        commands = manifest["features"]["slash_commands"]
        names = [c["command"] for c in commands]
        assert "/ledgr" in names

    def test_socket_mode_enabled(self, manifest):
        assert manifest["settings"]["socket_mode_enabled"] is True

    def test_interactivity_enabled(self, manifest):
        assert manifest["settings"]["interactivity"]["is_enabled"] is True

    def test_bot_user(self, manifest):
        bot = manifest["features"]["bot_user"]
        assert bot["display_name"] == "Ledgr"
        assert bot["always_online"] is True


# ---------------------------------------------------------------------------
# app.socket_run.run — raises SystemExit when tokens are missing
# ---------------------------------------------------------------------------

class TestSocketRun:
    def test_raises_systemexit_when_tokens_missing(self, monkeypatch):
        """run() must raise SystemExit listing missing vars; must NOT connect."""
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)

        # Patch SocketModeHandler to detect accidental connection attempts
        import app.socket_run as sr

        original_run = sr.run

        sentinel_called = []

        def _fake_handler(*args, **kwargs):
            sentinel_called.append(True)
            raise AssertionError("SocketModeHandler should not be instantiated when tokens are missing")

        monkeypatch.setattr(
            "slack_bolt.adapter.socket_mode.SocketModeHandler",
            _fake_handler,
            raising=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            sr.run()

        msg = str(exc_info.value)
        assert "SLACK_BOT_TOKEN" in msg
        assert "SLACK_APP_TOKEN" in msg
        assert not sentinel_called, "SocketModeHandler was instantiated unexpectedly"

    def test_systemexit_message_mentions_docs(self, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

        import app.socket_run as sr

        with pytest.raises(SystemExit) as exc_info:
            sr.run()

        assert "slack-setup.md" in str(exc_info.value)

    def test_no_systemexit_when_tokens_present(self, monkeypatch):
        """When tokens are set, run() should NOT raise SystemExit at the validation step."""
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-fake")

        import app.socket_run as sr

        # Patch SocketModeHandler so we never open a real connection,
        # but also stop execution after the guard passes.
        class _StopHere(Exception):
            pass

        def _fake_handler(*args, **kwargs):
            raise _StopHere("reached handler — guard passed")

        monkeypatch.setattr(
            "slack_bolt.adapter.socket_mode.SocketModeHandler",
            _fake_handler,
            raising=False,
        )

        with pytest.raises(_StopHere):
            sr.run()
