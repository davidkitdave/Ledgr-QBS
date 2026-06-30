"""Tests for multi-workspace OAuth manifest validation.

build_app / fastapi_app / build_oauth_settings were removed from app/slack_app
in the ADK-consolidation refactor (Tasks 3+4). The production HTTP entry point
is now build_fastapi_app() in accounting_agents/slack_runner.py — tested in
test_slack_runner.py.

This file retains the manifest-validation tests only.
"""

from __future__ import annotations

import json

from app.slack_app import BOT_SCOPES


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

    def test_has_five_bot_events(self):
        m = self._manifest()
        events = m["settings"]["event_subscriptions"]["bot_events"]
        assert set(events) == {
            "app_home_opened",
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
