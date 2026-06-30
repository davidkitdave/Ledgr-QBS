"""Hermetic tests for WS-6.2 context caching (static_instruction split + config)."""

from __future__ import annotations

from types import SimpleNamespace

from ledgr_slack.context_cache_config import (
    LEDGR_CONTEXT_CACHE_INTERVALS,
    LEDGR_CONTEXT_CACHE_MIN_TOKENS,
    LEDGR_CONTEXT_CACHE_TTL_SECONDS,
    cached_token_discount,
    ledgr_context_cache_config,
    log_context_cache_usage,
)


def test_ledgr_context_cache_config_defaults():
    cfg = ledgr_context_cache_config()
    assert cfg.min_tokens >= 2048
    assert cfg.ttl_seconds == LEDGR_CONTEXT_CACHE_TTL_SECONDS
    assert cfg.cache_intervals == LEDGR_CONTEXT_CACHE_INTERVALS
    assert cfg.min_tokens == LEDGR_CONTEXT_CACHE_MIN_TOKENS
    assert LEDGR_CONTEXT_CACHE_INTERVALS == 10


def test_log_context_cache_usage_emits_metrics(caplog):
    resp = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            cached_content_token_count=1500,
            prompt_token_count=2200,
        )
    )
    with caplog.at_level("INFO"):
        discount = log_context_cache_usage(resp, lane="test")
    assert discount == 1500


def test_cached_token_discount_zero_when_missing():
    resp = SimpleNamespace()
    assert cached_token_discount(resp) == 0
