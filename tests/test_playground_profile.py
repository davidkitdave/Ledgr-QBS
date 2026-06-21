"""Tests for the dev-playground default-profile seed and consolidate_node guard.

Hermetic — no Firestore, no Gemini, no Slack.

Covers:
(a) load_client_profile seeds expected state keys when no channel/profile resolves
    and the dev gate is on (LEDGR_ENV unset / "dev").
(b) load_client_profile does NOT seed when the gate is off (LEDGR_ENV="prod").
(c) load_client_profile does NOT seed when a real profile is already present in state.
(d) consolidate_node completes (no ValueError) when software is missing, using the
    default exporter ("qbs").
(e) consolidate_node uses the correct exporter when software is a valid value.
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from accounting_agents import nodes
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice


# --------------------------------------------------------------------------- #
# Helpers / fakes
# --------------------------------------------------------------------------- #

class FakeCallbackContext:
    """Minimal stand-in for ADK CallbackContext — only .state is needed."""

    def __init__(self, state: dict):
        self.state = dict(state)


def _make_fake_load_no_profile(callback_context):
    """Fake _load_client_by_channel that resolves nothing (simulates no channel)."""
    return None


def _make_fake_load_real_profile(callback_context):
    """Fake _load_client_by_channel that seeds a real Firestore-backed profile."""
    callback_context.state["client_id"] = "real-client-001"
    callback_context.state["client_name"] = "Real Client Pty Ltd"
    callback_context.state["software"] = "xero"
    return None


# --------------------------------------------------------------------------- #
# Task 1 — Dev-gated profile seed via load_client_profile
# --------------------------------------------------------------------------- #

class TestPlaygroundProfileSeed:
    def test_seed_populates_expected_keys_when_gate_on(self, monkeypatch, tmp_path):
        """Gate on (dev default): missing profile → synthetic ClientContext seeded.

        Phase 8 / playground-profile-my: when ``playground_profile.json``
        is present in the workspace it wins over the hardcoded defaults.
        This test isolates the pure-default path by pointing the loader at
        an empty directory so only the hardcoded ``_playground_default_context``
        values are written to state.
        """
        monkeypatch.delenv("LEDGR_ENV", raising=False)
        monkeypatch.delenv("LEDGR_PLAYGROUND_SEED", raising=False)
        # Force the JSON-loader to look at an empty dir so it can't see the
        # real playground_profile.json — isolates the hardcoded defaults.
        monkeypatch.setenv("LEDGR_PLAYGROUND_PROFILE_PATH", str(tmp_path / "no-such-profile.json"))

        ctx = FakeCallbackContext(state={})

        with patch("accounting_agents.agent._load_client_by_channel", _make_fake_load_no_profile):
            from accounting_agents.agent import load_client_profile
            result = load_client_profile(ctx)

        assert result is None  # ADK convention: always None
        # Core profile keys must be present (legacy hardcoded defaults).
        assert ctx.state.get("client_id") == "playground"
        assert ctx.state.get("client_name") == "Playground Client"
        assert ctx.state.get("software") == "qbs"
        assert ctx.state.get("region") == "SINGAPORE"
        assert ctx.state.get("base_currency") == "SGD"
        assert ctx.state.get("tax_registered") is True
        assert ctx.state.get("fye_month") == 12

    def test_seed_reads_jbi_plus_profile_from_json(self, monkeypatch):
        """Phase 8 / playground-profile-my: JSON profile wins over hardcoded defaults.

        Verifies the JBI PLUS / MALAYSIA / MYR seed profile (the default
        ADK web playground profile) loads into state when present in the
        workspace. COA must also be seeded so the categorize LLM runs.
        """
        monkeypatch.delenv("LEDGR_ENV", raising=False)
        monkeypatch.delenv("LEDGR_PLAYGROUND_SEED", raising=False)
        # Don't override LEDGR_PLAYGROUND_PROFILE_PATH — use the real
        # workspace file so this test catches any drift in the default seed.

        ctx = FakeCallbackContext(state={})

        with patch("accounting_agents.agent._load_client_by_channel", _make_fake_load_no_profile):
            from accounting_agents.agent import load_client_profile
            load_client_profile(ctx)

        # JBI PLUS / MALAYSIA profile wins.
        assert ctx.state.get("client_id") == "jbi-plus-auto"
        assert ctx.state.get("client_name") == "JBI PLUS AUTO SDN BHD"
        assert ctx.state.get("region") == "MALAYSIA"
        assert ctx.state.get("base_currency") == "MYR"
        # COA must be populated (else categorize LLM is skipped and
        # account_code ends up blank — the YAU LEE issue).
        coa = ctx.state.get("coa") or []
        assert len(coa) > 0, "playground_profile.json must seed a COA so categorize LLM runs"
        # entity_memory carries the YAU LEE vendor -> 500-020 mapping.
        em = ctx.state.get("entity_memory") or []
        assert any(e.get("name") == "YAU LEE MOTOR" for e in em), (
            "entity_memory must include the YAU LEE MOTOR mapping for tax"
        )

    def test_seed_does_not_activate_in_prod(self, monkeypatch):
        """Gate off (LEDGR_ENV=prod): no profile seeded even when channel resolves nothing."""
        monkeypatch.setenv("LEDGR_ENV", "prod")
        monkeypatch.delenv("LEDGR_PLAYGROUND_SEED", raising=False)

        ctx = FakeCallbackContext(state={})

        with patch("accounting_agents.agent._load_client_by_channel", _make_fake_load_no_profile):
            from accounting_agents import agent as agent_mod
            # Reload config helper to pick up monkeypatched env
            import importlib
            import accounting_agents.config as cfg_mod
            importlib.reload(cfg_mod)
            # Directly test via is_playground_seed_enabled rather than import cache
            from accounting_agents.config import is_playground_seed_enabled
            assert is_playground_seed_enabled() is False

            result = agent_mod.load_client_profile(ctx)

        assert result is None
        # No playground keys should have been written
        assert ctx.state.get("client_id") is None
        assert ctx.state.get("software") is None

    def test_seed_does_not_activate_when_explicit_flag_false(self, monkeypatch):
        """LEDGR_PLAYGROUND_SEED=false disables seed even in dev."""
        monkeypatch.delenv("LEDGR_ENV", raising=False)
        monkeypatch.setenv("LEDGR_PLAYGROUND_SEED", "false")

        from accounting_agents.config import is_playground_seed_enabled
        assert is_playground_seed_enabled() is False

    def test_seed_does_not_overwrite_existing_profile(self, monkeypatch):
        """Real profile already loaded by _load_client_by_channel → seed is skipped."""
        monkeypatch.delenv("LEDGR_ENV", raising=False)
        monkeypatch.delenv("LEDGR_PLAYGROUND_SEED", raising=False)

        ctx = FakeCallbackContext(state={})

        with patch("accounting_agents.agent._load_client_by_channel", _make_fake_load_real_profile):
            from accounting_agents.agent import load_client_profile
            load_client_profile(ctx)

        # Real profile values must be intact, playground defaults must NOT appear
        assert ctx.state.get("client_id") == "real-client-001"
        assert ctx.state.get("software") == "xero"
        assert ctx.state.get("client_name") == "Real Client Pty Ltd"

    def test_seed_does_not_overwrite_pre_existing_state_profile(self, monkeypatch):
        """If client_id is already in state before callback runs, seed is skipped."""
        monkeypatch.delenv("LEDGR_ENV", raising=False)
        monkeypatch.delenv("LEDGR_PLAYGROUND_SEED", raising=False)

        ctx = FakeCallbackContext(state={"client_id": "pre-existing", "software": "xero"})

        with patch("accounting_agents.agent._load_client_by_channel", _make_fake_load_no_profile):
            from accounting_agents.agent import load_client_profile
            load_client_profile(ctx)

        assert ctx.state.get("client_id") == "pre-existing"
        assert ctx.state.get("software") == "xero"

    def test_seed_uses_env_overrides(self, monkeypatch, tmp_path):
        """LEDGR_PLAYGROUND_* env vars override the hard-coded defaults."""
        monkeypatch.delenv("LEDGR_ENV", raising=False)
        monkeypatch.delenv("LEDGR_PLAYGROUND_SEED", raising=False)
        monkeypatch.setenv("LEDGR_PLAYGROUND_CLIENT_ID", "acme-001")
        monkeypatch.setenv("LEDGR_PLAYGROUND_CLIENT_NAME", "Acme Pte Ltd")
        monkeypatch.setenv("LEDGR_PLAYGROUND_CLIENT_UEN", "202312345A")
        monkeypatch.setenv("LEDGR_PLAYGROUND_SOFTWARE", "xero")
        monkeypatch.setenv("LEDGR_PLAYGROUND_CURRENCY", "MYR")
        monkeypatch.setenv("LEDGR_PLAYGROUND_REGION", "MALAYSIA")
        monkeypatch.setenv("LEDGR_PLAYGROUND_FYE_MONTH", "6")
        monkeypatch.setenv("LEDGR_PLAYGROUND_TAX_REGISTERED", "false")
        # Point the JSON-file path at a non-existent file so it can't override the env.
        monkeypatch.setenv("LEDGR_PLAYGROUND_PROFILE_PATH", str(tmp_path / "missing.json"))

        ctx = FakeCallbackContext(state={})
        with patch("accounting_agents.agent._load_client_by_channel", _make_fake_load_no_profile):
            from accounting_agents.agent import load_client_profile
            load_client_profile(ctx)

        assert ctx.state.get("client_id") == "acme-001"
        assert ctx.state.get("client_name") == "Acme Pte Ltd"
        assert ctx.state.get("client_uen") == "202312345A"
        assert ctx.state.get("software") == "xero"
        assert ctx.state.get("base_currency") == "MYR"
        assert ctx.state.get("region") == "MALAYSIA"
        assert ctx.state.get("fye_month") == 6
        assert ctx.state.get("tax_registered") is False

    def test_seed_uses_json_file_overrides(self, monkeypatch, tmp_path):
        """A ``playground_profile.json`` file overrides env vars (and defaults)."""
        import json as _json

        monkeypatch.delenv("LEDGR_ENV", raising=False)
        monkeypatch.delenv("LEDGR_PLAYGROUND_SEED", raising=False)
        monkeypatch.setenv("LEDGR_PLAYGROUND_CLIENT_NAME", "From Env")
        profile_path = tmp_path / "playground_profile.json"
        profile_path.write_text(_json.dumps({
            "client_id": "json-001",
            "client_name": "From JSON",
            "software": "qbs",
        }))
        monkeypatch.setenv("LEDGR_PLAYGROUND_PROFILE_PATH", str(profile_path))

        ctx = FakeCallbackContext(state={})
        with patch("accounting_agents.agent._load_client_by_channel", _make_fake_load_no_profile):
            from accounting_agents.agent import load_client_profile
            load_client_profile(ctx)

        # JSON wins over env when keys overlap
        assert ctx.state.get("client_name") == "From JSON"
        # JSON-only key
        assert ctx.state.get("client_id") == "json-001"


# --------------------------------------------------------------------------- #
# Task 1b — seed_playground_profile_if_needed helper (classify_node path)
#
# Tests the public helper that classify_node calls so the playground seed
# fires even when the coordinator/before_agent_callback is absent (WS3a fix).
# --------------------------------------------------------------------------- #

class TestSeedPlaygroundProfileIfNeeded:
    """Direct tests for seed_playground_profile_if_needed(state)."""

    def test_dev_empty_state_seeds_profile(self, monkeypatch, tmp_path):
        """Dev env + empty state → seeds region / base_currency / client_id."""
        monkeypatch.delenv("LEDGR_ENV", raising=False)
        monkeypatch.delenv("LEDGR_PLAYGROUND_SEED", raising=False)
        # Point at non-existent file so hardcoded defaults are used.
        monkeypatch.setenv(
            "LEDGR_PLAYGROUND_PROFILE_PATH", str(tmp_path / "no-profile.json")
        )

        from accounting_agents.agent import seed_playground_profile_if_needed

        state: dict = {}
        result = seed_playground_profile_if_needed(state)

        assert result is True
        assert state.get("client_id") == "playground"
        assert state.get("region") == "SINGAPORE"
        assert state.get("base_currency") == "SGD"

    def test_prod_env_does_not_seed(self, monkeypatch):
        """LEDGR_ENV=prod → helper returns False and state is unchanged."""
        monkeypatch.setenv("LEDGR_ENV", "prod")
        monkeypatch.delenv("LEDGR_PLAYGROUND_SEED", raising=False)

        from accounting_agents.agent import seed_playground_profile_if_needed

        state: dict = {}
        result = seed_playground_profile_if_needed(state)

        assert result is False
        assert state.get("client_id") is None
        assert state.get("region") is None

    def test_state_with_existing_client_id_is_not_overwritten(self, monkeypatch, tmp_path):
        """client_id already present → seed is skipped (idempotent / no clobber)."""
        monkeypatch.delenv("LEDGR_ENV", raising=False)
        monkeypatch.delenv("LEDGR_PLAYGROUND_SEED", raising=False)
        monkeypatch.setenv(
            "LEDGR_PLAYGROUND_PROFILE_PATH", str(tmp_path / "no-profile.json")
        )

        from accounting_agents.agent import seed_playground_profile_if_needed

        state: dict = {"client_id": "already-set", "client_name": "Existing Corp"}
        result = seed_playground_profile_if_needed(state)

        assert result is False
        assert state.get("client_id") == "already-set"
        assert state.get("client_name") == "Existing Corp"


# --------------------------------------------------------------------------- #
# Task 2 — consolidate_node graceful guard when software is missing
# --------------------------------------------------------------------------- #

def _make_invoice() -> NormalizedInvoice:
    return NormalizedInvoice(
        invoice_number="INV-001",
        invoice_date=date(2025, 3, 15),
        doc_total=109.0,
        reconciled=True,
        our_gst_registered=True,
        lines=[InvoiceLine(description="Goods", net_amount=100.0, gst_amount=9.0)],
    )


def _base_invoice_state(**overrides) -> dict:
    inv = _make_invoice()
    state: dict = {
        nodes.ARTIFACT_NAME_KEY: nodes.ARTIFACT_NAME_FMT.format(file_id="F1"),
        nodes.NORMALIZED_KEY: [nodes._inv_to_dict(inv)],
        nodes.DOC_TYPE_KEY: "invoice",
        nodes.ROUTES_KEY: [{"fy": "2025", "sheet": "Purchase"}],
        nodes.CLASSIFY_CONFIDENCE_KEY: 0.95,
        "op_id": "C1:F1",
        "client_id": "test-client",
        "client_name": "Test Client",
        "tax_registered": True,
        "direction": "purchase",
    }
    state.update(overrides)
    return state


class FakeConsolidateContext:
    def __init__(self, state: dict):
        self.state = dict(state)

    async def load_artifact(self, filename, version=None):
        inline = SimpleNamespace(data=b"%PDF stub", mime_type="application/pdf")
        return SimpleNamespace(inline_data=inline)


class TestConsolidateNodeGuard:
    def test_completes_without_software_key(self):
        """consolidate_node must not raise when 'software' is absent."""
        state = _base_invoice_state()
        assert "software" not in state

        ctx = FakeConsolidateContext(state=state)
        asyncio.run(nodes.consolidate_node._func(ctx))

        payload = ctx.state.get(nodes.LEDGER_ROWS_KEY)
        assert payload is not None
        assert payload["kind"] == "invoice"
        assert payload["software"] == ""
        assert payload["batches"] == []

    def test_completes_when_software_is_none(self):
        """consolidate_node must not raise when software=None."""
        state = _base_invoice_state(software=None)
        ctx = FakeConsolidateContext(state=state)
        asyncio.run(nodes.consolidate_node._func(ctx))

        payload = ctx.state[nodes.LEDGER_ROWS_KEY]
        assert payload["software"] == ""
        assert payload["batches"] == []

    def test_completes_when_software_is_empty_string(self):
        """consolidate_node must not raise when software=''."""
        state = _base_invoice_state(software="")
        ctx = FakeConsolidateContext(state=state)
        asyncio.run(nodes.consolidate_node._func(ctx))

        payload = ctx.state[nodes.LEDGER_ROWS_KEY]
        assert payload["software"] == ""
        assert payload["batches"] == []

    def test_uses_valid_software_when_provided(self):
        """consolidate_node respects software='xero' when it is valid."""
        state = _base_invoice_state(software="xero")
        ctx = FakeConsolidateContext(state=state)
        asyncio.run(nodes.consolidate_node._func(ctx))

        payload = ctx.state[nodes.LEDGER_ROWS_KEY]
        # Exporter was xero; the payload key reflects the original value
        # (nodes sets software = "qbs" only when it normalises away a bad value;
        # for valid "xero" the local var stays "xero" and the payload uses it)
        assert payload["software"] == "xero"

    def test_uses_valid_software_qbs(self):
        """consolidate_node respects software='qbs' when it is valid."""
        state = _base_invoice_state(software="qbs")
        ctx = FakeConsolidateContext(state=state)
        asyncio.run(nodes.consolidate_node._func(ctx))

        payload = ctx.state[nodes.LEDGER_ROWS_KEY]
        assert payload["software"] == "qbs"
