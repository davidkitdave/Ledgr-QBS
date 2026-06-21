"""Hermetic tests for WS-6.2 context caching (static_instruction split + config)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from invoice_processing.shared_libraries.context_cache_config import (
    LEDGR_CONTEXT_CACHE_INTERVALS,
    LEDGR_CONTEXT_CACHE_MIN_TOKENS,
    LEDGR_CONTEXT_CACHE_TTL_SECONDS,
    coa_cache_fingerprint,
    ledgr_context_cache_config,
    log_context_cache_usage,
)


def test_ledgr_context_cache_config_defaults():
    cfg = ledgr_context_cache_config()
    assert cfg.min_tokens >= 2048
    assert cfg.ttl_seconds == LEDGR_CONTEXT_CACHE_TTL_SECONDS
    assert cfg.cache_intervals == LEDGR_CONTEXT_CACHE_INTERVALS
    assert cfg.min_tokens == LEDGR_CONTEXT_CACHE_MIN_TOKENS


def test_coa_cache_fingerprint_stable_for_same_coa():
    coa = json.dumps([{"key": "6001", "description": "Office"}])
    assert coa_cache_fingerprint(coa, "gemini-2.0-flash") == coa_cache_fingerprint(
        coa, "gemini-2.0-flash"
    )


def test_coa_cache_fingerprint_differs_when_coa_changes():
    coa_a = json.dumps([{"key": "6001", "description": "Office"}])
    coa_b = json.dumps([{"key": "6200", "description": "Travel"}])
    assert coa_cache_fingerprint(coa_a, "gemini-2.0-flash") != coa_cache_fingerprint(
        coa_b, "gemini-2.0-flash"
    )


def test_coa_cache_fingerprint_differs_by_model():
    coa = json.dumps([{"key": "6001"}])
    assert coa_cache_fingerprint(coa, "gemini-2.0-flash") != coa_cache_fingerprint(
        coa, "gemini-2.5-flash"
    )


def test_log_context_cache_usage_emits_metrics(caplog):
    resp = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            cached_content_token_count=1500,
            prompt_token_count=2200,
        )
    )
    with caplog.at_level("INFO"):
        log_context_cache_usage(resp, lane="extract")
    assert any("cached_content_token_count=1500" in r.message for r in caplog.records)
    assert any("lane=extract" in r.message for r in caplog.records)


def test_extract_uses_system_instruction_for_static_prompt(monkeypatch):
    """Static faithful rules live in system_instruction, not duplicated in contents."""
    import invoice_processing.extract.ledger_extract as le
    import invoice_processing.shared_libraries.genai_client as gc

    captured: dict = {}

    def fake_generate(model=None, contents=None, config=None, **_kw):
        captured["contents"] = contents
        captured["config"] = config
        return SimpleNamespace(
            text='{"documents": []}',
            usage_metadata=SimpleNamespace(
                cached_content_token_count=0,
                prompt_token_count=100,
            ),
        )

    fake_client = SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
    monkeypatch.setattr(gc, "make_client", lambda *a, **kw: fake_client)
    monkeypatch.setattr(le, "make_client", lambda *a, **kw: fake_client)

    le.extract_document_ledger(
        b"%PDF-1.4",
        "application/pdf",
        client_name="Acme Pte Ltd",
        client_uen="201234567A",
        hint="focus on page 2",
    )

    assert captured, "generate_content was not called"
    config = captured["config"]
    assert config.system_instruction, "static rules must be in system_instruction"
    static = config.system_instruction
    assert "transcribing financial documents faithfully" in static.lower()
    assert "do not collapse rows" in static.lower()

    contents = captured["contents"]
    contents_text = contents if isinstance(contents, str) else str(contents)
    assert "transcribing financial documents faithfully" not in contents_text.lower()
    assert "Acme Pte Ltd" in contents_text or "201234567A" in contents_text


def test_coa_llm_uses_system_instruction_for_static_rules(monkeypatch):
    """COA categorization static rules live in system_instruction."""
    import invoice_processing.export.categorizer as cat
    import invoice_processing.shared_libraries.genai_client as gc

    captured: dict = {}

    def fake_generate(model=None, contents=None, config=None, **_kw):
        captured["contents"] = contents
        captured["config"] = config
        return SimpleNamespace(text=json.dumps({"results": []}))

    fake_client = SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
    monkeypatch.setattr(gc, "make_client", lambda *a, **kw: fake_client)

    sample_coa = [
        cat.CoaAccount(code="6001", description="Office Expenses", keywords="office"),
    ]
    unresolved = [(0, "Paper supplies", "Vendor-A")]
    cat._llm_match_lines(unresolved, sample_coa, model=None, tax_registered=True)

    assert captured, "generate_content was not called"
    config = captured["config"]
    assert config.system_instruction
    static = config.system_instruction
    assert "chart of accounts" in static.lower() or "coa" in static.lower()
    assert cat.UNMAPPED_ACCOUNT_CODE in static

    contents_text = (
        captured["contents"]
        if isinstance(captured["contents"], str)
        else str(captured["contents"])
    )
    assert "6001" in contents_text
    assert "Paper supplies" in contents_text
    assert cat.UNMAPPED_ACCOUNT_CODE not in contents_text or "abstain" not in static.lower()


def test_document_app_has_context_cache_config():
    from accounting_agents.agent import assistant_app, document_app

    assert document_app.context_cache_config is not None
    assert document_app.context_cache_config.min_tokens >= 2048
    assert assistant_app.context_cache_config is not None
