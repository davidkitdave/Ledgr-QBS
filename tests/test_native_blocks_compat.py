"""Tests for app/native_blocks_compat.py."""

from __future__ import annotations

import pytest

import app.native_blocks_compat as compat


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear the probe cache before every test."""
    compat._reset_for_tests()
    yield
    compat._reset_for_tests()


class TestForcedOn:
    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "YES"])
    def test_always_true(self, monkeypatch, value):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", value)
        assert compat.supports_native_blocks() is True
        assert compat.supports_native_blocks(channel_id="C123") is True

    def test_cache_ignored_when_forced_on(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "1")
        compat.record_probe_result("C123", False)
        assert compat.supports_native_blocks(channel_id="C123") is True


class TestForcedOff:
    @pytest.mark.parametrize("value", ["0", "false", "False", "FALSE", "no", "NO"])
    def test_always_false(self, monkeypatch, value):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", value)
        assert compat.supports_native_blocks() is False
        assert compat.supports_native_blocks(channel_id="C123") is False

    def test_cache_ignored_when_forced_off(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "0")
        compat.record_probe_result("C123", True)
        assert compat.supports_native_blocks(channel_id="C123") is False


class TestAutoMode:
    def test_auto_explicit_no_cache_returns_true(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "auto")
        assert compat.supports_native_blocks() is True
        assert compat.supports_native_blocks(channel_id="C999") is True

    def test_unset_behaves_like_auto(self, monkeypatch):
        monkeypatch.delenv("LEDGR_NATIVE_BLOCKS", raising=False)
        assert compat.supports_native_blocks() is True
        assert compat.supports_native_blocks(channel_id="C999") is True

    def test_cached_false_returned_for_that_channel(self, monkeypatch):
        monkeypatch.delenv("LEDGR_NATIVE_BLOCKS", raising=False)
        compat.record_probe_result("C123", False)
        assert compat.supports_native_blocks(channel_id="C123") is False

    def test_cached_false_does_not_affect_other_channel(self, monkeypatch):
        monkeypatch.delenv("LEDGR_NATIVE_BLOCKS", raising=False)
        compat.record_probe_result("C123", False)
        assert compat.supports_native_blocks(channel_id="C456") is True

    def test_cached_false_does_not_affect_no_channel(self, monkeypatch):
        monkeypatch.delenv("LEDGR_NATIVE_BLOCKS", raising=False)
        compat.record_probe_result("C123", False)
        assert compat.supports_native_blocks() is True

    def test_cached_true_returned(self, monkeypatch):
        monkeypatch.delenv("LEDGR_NATIVE_BLOCKS", raising=False)
        compat.record_probe_result("C123", True)
        assert compat.supports_native_blocks(channel_id="C123") is True

    def test_multiple_channels_independent(self, monkeypatch):
        monkeypatch.delenv("LEDGR_NATIVE_BLOCKS", raising=False)
        compat.record_probe_result("C_YES", True)
        compat.record_probe_result("C_NO", False)
        assert compat.supports_native_blocks(channel_id="C_YES") is True
        assert compat.supports_native_blocks(channel_id="C_NO") is False
        assert compat.supports_native_blocks(channel_id="C_UNKNOWN") is True


class TestConstants:
    def test_native_block_types_is_tuple(self):
        assert isinstance(compat.NATIVE_BLOCK_TYPES, tuple)

    def test_expected_types_present(self):
        for block_type in ("plan", "card", "carousel", "data_table", "context_actions",
                           "task_card", "alert"):
            assert block_type in compat.NATIVE_BLOCK_TYPES
