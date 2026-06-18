"""Tests for app/commands.py and /ledgr slash-command wiring.

All tests use InMemoryClientStore and fake ack/client objects.
No live Slack token, Firestore, or Gemini call is made.
"""

from __future__ import annotations


from app.commands import (
    ledgr_slash_command_name,
    parse_ledgr_command,
    settings_prefill,
)
from app.slack_app import handle_ledgr_command, handle_onboarding_submit
from invoice_processing.export.client_context import ClientContext, InMemoryClientStore


# --------------------------------------------------------------------------- #
# Shared fakes (mirrors test_app_slack.py pattern)
# --------------------------------------------------------------------------- #

class FakeAck:
    def __init__(self):
        self.called = False

    def __call__(self, *args, **kwargs):
        self.called = True


class FakeClient:
    def __init__(self):
        self.posted_messages: list[dict] = []
        self.opened_views: list[dict] = []

    def chat_postMessage(self, **kwargs):
        self.posted_messages.append(kwargs)
        return {"ok": True}

    def views_open(self, **kwargs):
        self.opened_views.append(kwargs)
        return {"ok": True}


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
                "selected_option": {
                    "text": {"type": "plain_text", "text": "December"},
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


def _submit_body(
    channel_id: str = "C-TEST-1",
    team_id: str = "T-TEAM-1",
    client_name: str = "TestCo Pte Ltd",
    fye_month: str = "12",
    accounting_software: str = "QBS Ledger",
    gst_value: str = "yes",
) -> dict:
    return {
        "team": {"id": team_id},
        "view": {
            "callback_id": "ledgr_onboarding",
            "private_metadata": channel_id,
            "state": {
                "values": _view_state_values(client_name, fye_month, accounting_software, gst_value)
            },
        },
    }


# --------------------------------------------------------------------------- #
# parse_ledgr_command
# --------------------------------------------------------------------------- #

class TestParseLedgrCommand:

    def test_empty_string_returns_help(self):
        cmd = parse_ledgr_command("")
        assert cmd.subcommand == "help"

    def test_none_returns_help(self):
        cmd = parse_ledgr_command(None)
        assert cmd.subcommand == "help"

    def test_help_returns_help(self):
        cmd = parse_ledgr_command("help")
        assert cmd.subcommand == "help"

    def test_settings_returns_settings(self):
        cmd = parse_ledgr_command("settings")
        assert cmd.subcommand == "settings"

    def test_export_returns_export(self):
        cmd = parse_ledgr_command("export")
        assert cmd.subcommand == "export"

    def test_parse_profile_subcommand(self):
        from app.commands import parse_ledgr_command
        assert parse_ledgr_command("profile").subcommand == "profile"


class TestLedgrSlashCommandName:
    def test_dev_default(self, monkeypatch):
        monkeypatch.delenv("LEDGR_SLASH_COMMAND", raising=False)
        monkeypatch.setenv("LEDGR_ENV", "dev")
        assert ledgr_slash_command_name() == "/ledgr-dev"

    def test_prod(self, monkeypatch):
        monkeypatch.delenv("LEDGR_SLASH_COMMAND", raising=False)
        monkeypatch.setenv("LEDGR_ENV", "prod")
        assert ledgr_slash_command_name() == "/ledgr"

    def test_override(self, monkeypatch):
        monkeypatch.setenv("LEDGR_SLASH_COMMAND", "/ledgr-test")
        assert ledgr_slash_command_name() == "/ledgr-test"

    def test_bogus_returns_help(self):
        cmd = parse_ledgr_command("bogus")
        assert cmd.subcommand == "help"

    def test_unknown_with_args_returns_help(self):
        cmd = parse_ledgr_command("whatever foo bar")
        assert cmd.subcommand == "help"

    def test_settings_extra_args_captured(self):
        cmd = parse_ledgr_command("settings extra arg")
        assert cmd.subcommand == "settings"
        assert cmd.args == ["extra", "arg"]

    def test_export_no_args(self):
        cmd = parse_ledgr_command("export")
        assert cmd.args == []

    def test_case_insensitive(self):
        assert parse_ledgr_command("SETTINGS").subcommand == "settings"
        assert parse_ledgr_command("EXPORT").subcommand == "export"
        assert parse_ledgr_command("HELP").subcommand == "help"

    def test_whitespace_trimmed(self):
        assert parse_ledgr_command("  settings  ").subcommand == "settings"


# --------------------------------------------------------------------------- #
# settings_prefill
# --------------------------------------------------------------------------- #

class TestSettingsPrefill:

    def test_none_returns_none(self):
        assert settings_prefill(None) is None

    def test_tax_registered_true_maps_to_yes(self):
        ctx = ClientContext(
            client_id="c1",
            client_name="Acme",
            fye_month=3,
            accounting_software="Xero",
            tax_registered=True,
        )
        result = settings_prefill(ctx)
        assert result is not None
        assert result["gst_registered"] == "yes"

    def test_tax_registered_false_maps_to_no(self):
        ctx = ClientContext(
            client_id="c1",
            client_name="Acme",
            fye_month=3,
            accounting_software="Xero",
            tax_registered=False,
        )
        result = settings_prefill(ctx)
        assert result["gst_registered"] == "no"

    def test_fye_month_returned_as_string(self):
        ctx = ClientContext(
            client_id="c1",
            client_name="Acme",
            fye_month=3,
            accounting_software="Xero",
            tax_registered=True,
        )
        result = settings_prefill(ctx)
        assert result["fye_month"] == "3"

    def test_client_name_preserved(self):
        ctx = ClientContext(
            client_id="c1",
            client_name="Acme",
            fye_month=3,
            accounting_software="Xero",
            tax_registered=True,
        )
        result = settings_prefill(ctx)
        assert result["client_name"] == "Acme"

    def test_accounting_software_preserved(self):
        ctx = ClientContext(
            client_id="c1",
            client_name="Acme",
            fye_month=3,
            accounting_software="Xero",
            tax_registered=True,
        )
        result = settings_prefill(ctx)
        assert result["accounting_software"] == "Xero"

    def test_fye_month_none_returns_none_value(self):
        ctx = ClientContext(
            client_id="c1",
            client_name="Acme",
            fye_month=None,
            accounting_software="QBS Ledger",
            tax_registered=True,
        )
        result = settings_prefill(ctx)
        assert result["fye_month"] is None


# --------------------------------------------------------------------------- #
# handle_ledgr_command
# --------------------------------------------------------------------------- #

def _ledgr_body(text: str = "", channel_id: str = "C-LEDGR", trigger_id: str = "trig-1") -> dict:
    return {
        "text": text,
        "channel_id": channel_id,
        "trigger_id": trigger_id,
    }


class TestHandleLedgrCommandSettings:

    def _existing_store(self, channel_id: str = "C-LEDGR") -> InMemoryClientStore:
        store = InMemoryClientStore()
        ctx = ClientContext(
            client_id="client-existing",
            client_name="ExistingCo",
            fye_month=6,
            accounting_software="Xero",
            tax_registered=True,
            status="active",
        )
        store.add(ctx, channel_id=channel_id)
        return store

    def test_acks(self):
        store = self._existing_store()
        ack = FakeAck()
        handle_ledgr_command(ack, _ledgr_body("settings"), FakeClient(), store)
        assert ack.called

    def test_opens_modal_with_existing_profile(self):
        store = self._existing_store()
        client = FakeClient()
        handle_ledgr_command(FakeAck(), _ledgr_body("settings"), client, store)
        assert len(client.opened_views) == 1

    def test_modal_private_metadata_is_channel_id(self):
        store = self._existing_store(channel_id="C-LEDGR")
        client = FakeClient()
        handle_ledgr_command(FakeAck(), _ledgr_body("settings", channel_id="C-LEDGR"), client, store)
        view = client.opened_views[0]["view"]
        assert view["private_metadata"] == "C-LEDGR"

    def test_modal_callback_id_is_ledgr_onboarding(self):
        store = self._existing_store()
        client = FakeClient()
        handle_ledgr_command(FakeAck(), _ledgr_body("settings"), client, store)
        view = client.opened_views[0]["view"]
        assert view["callback_id"] == "ledgr_onboarding"

    def test_modal_prefilled_client_name(self):
        store = self._existing_store()
        client = FakeClient()
        handle_ledgr_command(FakeAck(), _ledgr_body("settings"), client, store)
        view = client.opened_views[0]["view"]
        # client_name block should have initial_value = "ExistingCo"
        client_name_block = next(b for b in view["blocks"] if b["block_id"] == "client_name")
        assert client_name_block["element"].get("initial_value") == "ExistingCo"

    def test_settings_no_profile_opens_blank_modal(self):
        store = InMemoryClientStore()  # no profile for channel
        client = FakeClient()
        handle_ledgr_command(FakeAck(), _ledgr_body("settings", channel_id="C-NEW"), client, store)
        assert len(client.opened_views) == 1
        # blank modal: no initial_value on client_name element
        view = client.opened_views[0]["view"]
        client_name_block = next(b for b in view["blocks"] if b["block_id"] == "client_name")
        assert "initial_value" not in client_name_block["element"]


class TestHandleLedgrCommandExport:

    def test_acks(self):
        ack = FakeAck()
        handle_ledgr_command(ack, _ledgr_body("export"), FakeClient(), InMemoryClientStore())
        assert ack.called

    def test_posts_export_unavailable_blocks(self):
        from app.blocks import export_unavailable_blocks
        client = FakeClient()
        handle_ledgr_command(FakeAck(), _ledgr_body("export", channel_id="C-EXP"), client, InMemoryClientStore())
        assert len(client.posted_messages) == 1
        msg = client.posted_messages[0]
        assert msg["channel"] == "C-EXP"
        assert msg["blocks"] == export_unavailable_blocks()

    def test_does_not_open_modal(self):
        client = FakeClient()
        handle_ledgr_command(FakeAck(), _ledgr_body("export"), client, InMemoryClientStore())
        assert len(client.opened_views) == 0


class TestHandleLedgrCommandHelp:

    def test_help_explicit(self):
        from app.blocks import ledgr_help_blocks
        client = FakeClient()
        handle_ledgr_command(FakeAck(), _ledgr_body("help", channel_id="C-HELP"), client, InMemoryClientStore())
        assert client.posted_messages[0]["blocks"] == ledgr_help_blocks()

    def test_empty_text_posts_help(self):
        from app.blocks import ledgr_help_blocks
        client = FakeClient()
        handle_ledgr_command(FakeAck(), _ledgr_body("", channel_id="C-HELP"), client, InMemoryClientStore())
        assert client.posted_messages[0]["blocks"] == ledgr_help_blocks()

    def test_unknown_subcommand_posts_help(self):
        from app.blocks import ledgr_help_blocks
        client = FakeClient()
        handle_ledgr_command(FakeAck(), _ledgr_body("bogus"), client, InMemoryClientStore())
        assert client.posted_messages[0]["blocks"] == ledgr_help_blocks()

    def test_help_channel_correct(self):
        client = FakeClient()
        handle_ledgr_command(FakeAck(), _ledgr_body("help", channel_id="C-XYZ"), client, InMemoryClientStore())
        assert client.posted_messages[0]["channel"] == "C-XYZ"


class TestHandleLedgrCommandProfile:

    def test_acks(self):
        store = InMemoryClientStore()
        ack = FakeAck()
        handle_ledgr_command(ack, _ledgr_body("profile"), FakeClient(), store)
        assert ack.called

    def test_ledgr_profile_posts_summary(self):
        from app.slack_app import handle_ledgr_command
        from invoice_processing.export.client_context import ClientContext

        posted = []

        class _Client:
            def chat_postMessage(self, **kw): posted.append(kw)

        class _Store:
            def get_by_channel(self, cid):
                return ClientContext(
                    client_id="CL-1",
                    client_name="Company-A",
                    accounting_software="Xero",
                    fye_month=10,
                    tax_registered=False,
                )

        handle_ledgr_command(
            ack=lambda: None,
            body={"channel_id": "C1", "text": "profile"},
            client=_Client(),
            store=_Store(),
        )
        joined = " ".join(
            blk.get("text", {}).get("text", "")
            for call in posted
            for blk in call.get("blocks", [])
            if isinstance(blk.get("text"), dict)
        )
        assert "Company-A" in joined and "Xero" in joined

    def test_ledgr_profile_no_client_posts_guidance(self):
        from app.slack_app import handle_ledgr_command

        posted = []

        class _Client:
            def chat_postMessage(self, **kw): posted.append(kw)

        handle_ledgr_command(
            ack=lambda: None,
            body={"channel_id": "C-EMPTY", "text": "profile"},
            client=_Client(),
            store=InMemoryClientStore(),
        )
        assert len(posted) == 1
        text = posted[0].get("text", "")
        assert "settings" in text
        assert "blocks" not in posted[0] or posted[0]["blocks"] is None


# --------------------------------------------------------------------------- #
# FIX 1 regression: edit must not fork a new client
# --------------------------------------------------------------------------- #

class TestOnboardingSubmitEditPreservesClient:

    def _run_submit(self, store, channel_id="C1", client_name="NewName", id_factory=None):
        ack = FakeAck()
        client = FakeClient()
        body = _submit_body(channel_id=channel_id, client_name=client_name)
        if id_factory is None:
            id_factory = lambda: "should-not-be-used"  # noqa: E731 — short sentinel; def would add noise
        handle_onboarding_submit(body, ack, client, store, id_factory)
        return store, ack, client

    def test_edit_reuses_existing_client_id(self):
        store = InMemoryClientStore()
        # First submit: create the profile
        handle_onboarding_submit(
            _submit_body(channel_id="C1", client_name="Original"),
            FakeAck(), FakeClient(), store, lambda: "client-X",
        )
        assert store.get_by_channel("C1").client_id == "client-X"

        # Second submit (edit): should reuse client-X, NOT call the new factory
        handle_onboarding_submit(
            _submit_body(channel_id="C1", client_name="Updated"),
            FakeAck(), FakeClient(), store, lambda: "client-NEW",
        )
        ctx = store.get_by_channel("C1")
        assert ctx.client_id == "client-X", "edit must not fork a new client_id"

    def test_edit_preserves_active_status(self):
        store = InMemoryClientStore()
        # Create profile then manually advance it to "active"
        handle_onboarding_submit(
            _submit_body(channel_id="C1", client_name="Original"),
            FakeAck(), FakeClient(), store, lambda: "client-X",
        )
        store.set_status("client-X", "active")

        # Edit: status must stay "active"
        handle_onboarding_submit(
            _submit_body(channel_id="C1", client_name="Updated"),
            FakeAck(), FakeClient(), store, lambda: "client-NEW",
        )
        ctx = store.get_by_channel("C1")
        assert ctx.status == "active", "edit must not reset status to pending_coa"

    def test_edit_updates_client_name(self):
        store = InMemoryClientStore()
        handle_onboarding_submit(
            _submit_body(channel_id="C1", client_name="Original"),
            FakeAck(), FakeClient(), store, lambda: "client-X",
        )
        handle_onboarding_submit(
            _submit_body(channel_id="C1", client_name="Updated Name"),
            FakeAck(), FakeClient(), store, lambda: "client-NEW",
        )
        ctx = store.get_by_channel("C1")
        assert ctx.client_name == "Updated Name"

    def test_edit_preserves_category_mapping(self):
        store = InMemoryClientStore()
        handle_onboarding_submit(
            _submit_body(channel_id="C1", client_name="Original"),
            FakeAck(), FakeClient(), store, lambda: "client-X",
        )
        # Simulate a COA ingest that populated category_mapping
        ctx = store.get_by_channel("C1")
        ctx.category_mapping["Sales"] = "4000"

        # Edit: category_mapping must be preserved
        handle_onboarding_submit(
            _submit_body(channel_id="C1", client_name="Updated"),
            FakeAck(), FakeClient(), store, lambda: "client-NEW",
        )
        ctx_after = store.get_by_channel("C1")
        assert ctx_after.category_mapping.get("Sales") == "4000"

    def test_new_channel_uses_id_factory(self):
        store = InMemoryClientStore()
        handle_onboarding_submit(
            _submit_body(channel_id="C-BRAND-NEW", client_name="Fresh"),
            FakeAck(), FakeClient(), store, lambda: "client-FACTORY",
        )
        ctx = store.get_by_channel("C-BRAND-NEW")
        assert ctx.client_id == "client-FACTORY"
