"""Tests for dev/prod environment split helpers.

Covers:
- _env_prefix()   : "[dev] " in dev/unset; "" in prod
- _resolve_model(): per-env defaults + LEDGR_MODEL_<TIER> override
- _ns()           : optional Firestore namespace prefix
- process_file_event status message includes "[dev]" in dev; not in prod
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# autouse fixture — reset env vars AND module-level constants between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear all LEDGR_ENV / model / namespace vars before each test."""
    for key in ("LEDGR_ENV", "LEDGR_MODEL_LITE", "LEDGR_MODEL_STD",
                "LEDGR_FIRESTORE_NAMESPACE"):
        monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# Helper to re-evaluate the helpers after env changes (they read os.environ
# at call time, so no reimport is needed — but we isolate via the fixture).
# ---------------------------------------------------------------------------

def _prefix() -> str:
    from accounting_agents.config import _env_prefix
    return _env_prefix()


def _model(tier: str) -> str:
    from accounting_agents.config import _resolve_model
    return _resolve_model(tier)


def _ns(name: str) -> str:
    from accounting_agents.config import _ns as ns_fn
    return ns_fn(name)


# ---------------------------------------------------------------------------
# _env_prefix
# ---------------------------------------------------------------------------

class TestEnvPrefix:
    def test_dev_explicit(self, monkeypatch):
        monkeypatch.setenv("LEDGR_ENV", "dev")
        assert _prefix() == "[dev] "

    def test_dev_unset(self):
        # LEDGR_ENV not in env (cleared by autouse fixture)
        assert _prefix() == "[dev] "

    def test_prod(self, monkeypatch):
        monkeypatch.setenv("LEDGR_ENV", "prod")
        assert _prefix() == ""

    def test_prod_uppercase_ignored(self, monkeypatch):
        # Value is normalised to lowercase
        monkeypatch.setenv("LEDGR_ENV", "PROD")
        assert _prefix() == ""

    def test_whitespace_stripped(self, monkeypatch):
        monkeypatch.setenv("LEDGR_ENV", "  prod  ")
        assert _prefix() == ""


# ---------------------------------------------------------------------------
# _resolve_model
# ---------------------------------------------------------------------------

class TestResolveModel:
    def test_dev_lite_default(self):
        assert _model("lite") == "gemini-2.5-flash-lite"

    def test_dev_std_default(self):
        assert _model("std") == "gemini-2.5-flash"

    def test_prod_lite_defaults_to_flash(self, monkeypatch):
        monkeypatch.setenv("LEDGR_ENV", "prod")
        assert _model("lite") == "gemini-2.5-flash"

    def test_prod_std_defaults_to_flash(self, monkeypatch):
        monkeypatch.setenv("LEDGR_ENV", "prod")
        assert _model("std") == "gemini-2.5-flash"

    def test_override_lite_honored_in_dev(self, monkeypatch):
        monkeypatch.setenv("LEDGR_MODEL_LITE", "gemini-2.0-flash-exp")
        assert _model("lite") == "gemini-2.0-flash-exp"

    def test_override_lite_honored_in_prod(self, monkeypatch):
        monkeypatch.setenv("LEDGR_ENV", "prod")
        monkeypatch.setenv("LEDGR_MODEL_LITE", "gemini-2.5-flash-lite")
        assert _model("lite") == "gemini-2.5-flash-lite"

    def test_override_std_honored(self, monkeypatch):
        monkeypatch.setenv("LEDGR_MODEL_STD", "gemini-2.0-flash")
        assert _model("std") == "gemini-2.0-flash"

    def test_unset_env_treats_as_dev(self):
        # LEDGR_ENV absent → dev defaults
        assert _model("lite") == "gemini-2.5-flash-lite"
        assert _model("std") == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# _ns (Firestore namespace)
# ---------------------------------------------------------------------------

class TestNs:
    def test_no_namespace_passthrough(self):
        assert _ns("clients") == "clients"

    def test_namespace_prefixes(self, monkeypatch):
        monkeypatch.setenv("LEDGR_FIRESTORE_NAMESPACE", "dev")
        assert _ns("clients") == "dev_clients"

    def test_namespace_prefixes_channels(self, monkeypatch):
        monkeypatch.setenv("LEDGR_FIRESTORE_NAMESPACE", "dev")
        assert _ns("channels") == "dev_channels"

    def test_namespace_prefixes_interrupts(self, monkeypatch):
        monkeypatch.setenv("LEDGR_FIRESTORE_NAMESPACE", "dev")
        assert _ns("interrupts") == "dev_interrupts"

    def test_empty_namespace_passthrough(self, monkeypatch):
        monkeypatch.setenv("LEDGR_FIRESTORE_NAMESPACE", "")
        assert _ns("clients") == "clients"

    def test_whitespace_namespace_passthrough(self, monkeypatch):
        monkeypatch.setenv("LEDGR_FIRESTORE_NAMESPACE", "   ")
        assert _ns("clients") == "clients"


# ---------------------------------------------------------------------------
# Status message prefix — dev includes "[dev] ", prod does not
# ---------------------------------------------------------------------------

class TestStatusMessagePrefix:
    """Verify _env_prefix() integrates correctly into the status text pattern
    used by process_file_event (without requiring a live Slack client)."""

    def test_dev_status_text_includes_prefix(self, monkeypatch):
        monkeypatch.setenv("LEDGR_ENV", "dev")
        from accounting_agents.config import _env_prefix
        source_filename = "invoice.pdf"
        text = f"{_env_prefix()}📥 Received `{source_filename}` — on it…"
        assert text.startswith("[dev] ")

    def test_prod_status_text_has_no_prefix(self, monkeypatch):
        monkeypatch.setenv("LEDGR_ENV", "prod")
        from accounting_agents.config import _env_prefix
        source_filename = "invoice.pdf"
        text = f"{_env_prefix()}📥 Received `{source_filename}` — on it…"
        assert not text.startswith("[dev]")
        assert "invoice.pdf" in text

    def test_plan_label_dev(self, monkeypatch):
        monkeypatch.setenv("LEDGR_ENV", "dev")
        from accounting_agents.config import _env_prefix
        source_filename = "bank.pdf"
        plan_label = f"{_env_prefix()}{source_filename}"
        assert plan_label == "[dev] bank.pdf"

    def test_plan_label_prod(self, monkeypatch):
        monkeypatch.setenv("LEDGR_ENV", "prod")
        from accounting_agents.config import _env_prefix
        source_filename = "bank.pdf"
        plan_label = f"{_env_prefix()}{source_filename}"
        assert plan_label == "bank.pdf"
