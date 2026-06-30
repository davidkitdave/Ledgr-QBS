"""Tests for the single model_config module (env → model id)."""

from __future__ import annotations

import pytest

from ledgr_slack import model_config as mc


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    for key in (
        "LEDGR_MODEL_LITE",
        "LEDGR_MODEL_STD",
        "LEDGR_MODEL_CHAT",
        "LEDGR_MODEL_READ",
        "GEMINI_FLASH_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


class TestResolveModel:
    def test_defaults(self):
        assert mc.resolve_model("lite") == "gemini-2.5-flash-lite"
        assert mc.resolve_model("std") == "gemini-2.5-flash"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("LEDGR_MODEL_LITE", "custom-lite")
        monkeypatch.setenv("LEDGR_MODEL_STD", "custom-std")
        assert mc.lite_model() == "custom-lite"
        assert mc.std_model() == "custom-std"

    def test_legacy_gemini_flash_fallback_for_std(self, monkeypatch):
        monkeypatch.setenv("GEMINI_FLASH_MODEL", "legacy-flash")
        assert mc.std_model() == "legacy-flash"

    def test_ledgr_std_wins_over_legacy(self, monkeypatch):
        monkeypatch.setenv("LEDGR_MODEL_STD", "new-std")
        monkeypatch.setenv("GEMINI_FLASH_MODEL", "legacy-flash")
        assert mc.std_model() == "new-std"

    def test_chat_defaults_to_std(self, monkeypatch):
        assert mc.chat_model() == "gemini-2.5-flash"

    def test_read_defaults_to_lite(self, monkeypatch):
        assert mc.read_model() == "gemini-2.5-flash-lite"
