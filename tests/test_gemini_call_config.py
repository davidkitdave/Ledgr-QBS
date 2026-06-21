"""WS-6.3: default Gemini call config disables thinking on the easy path."""

from __future__ import annotations

import importlib
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from invoice_processing.shared_libraries.gemini_call_config import (
    ABSTAIN_BOUNDARY_THINKING_BUDGET,
    DEFAULT_THINKING_BUDGET,
    abstain_boundary_llm_config,
    default_llm_config,
)


def test_default_llm_config_sets_thinking_budget_zero():
    config = default_llm_config(temperature=0)
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_budget == DEFAULT_THINKING_BUDGET
    assert config.temperature == 0


def test_default_llm_config_overrides_thinking_config():
    from google.genai import types

    custom = types.ThinkingConfig(thinking_budget=512)
    config = default_llm_config(thinking_config=custom)
    assert config.thinking_config.thinking_budget == 512


def test_abstain_boundary_llm_config_uses_small_budget():
    config = abstain_boundary_llm_config(temperature=0)
    assert config.thinking_config.thinking_budget == ABSTAIN_BOUNDARY_THINKING_BUDGET


@pytest.mark.parametrize(
    "module_path,func_name,call_kwargs",
    [
        (
            "invoice_processing.extract.ledger_extract",
            "extract_document_ledger",
            {"data": b"%PDF-1.4", "mime_type": "application/pdf"},
        ),
        (
            "invoice_processing.classify.document_classifier",
            "classify_document",
            {"data": b"img", "mime_type": "image/png"},
        ),
        (
            "invoice_processing.export.categorizer",
            "_llm_match_lines",
            {
                "unresolved": [(0, "Paper", "Vendor")],
                "coa": [SimpleNamespace(key="6001", description="Office", account_type="")],
                "model": None,
            },
        ),
    ],
)
def test_easy_path_calls_disable_thinking(module_path, func_name, call_kwargs):
    mod = importlib.import_module(module_path)
    captured: dict = {}

    def fake_generate(model=None, contents=None, config=None, **_kw):
        captured["config"] = config
        if func_name == "_llm_match_lines":
            return SimpleNamespace(text='{"results": []}', candidates=[])
        if func_name == "classify_document":
            from invoice_processing.classify.document_classifier import ClassificationResult

            payload = ClassificationResult(
                doc_type="invoice",
                processable=True,
                issuer_name="A",
                bill_to_name="B",
                currency="SGD",
                total_amount=1.0,
                confidence=0.9,
                reason="test",
            )
            return SimpleNamespace(text=payload.model_dump_json())
        return SimpleNamespace(
            text='{"documents": []}',
            usage_metadata=SimpleNamespace(
                cached_content_token_count=0,
                prompt_token_count=1,
            ),
        )

    fake_client = SimpleNamespace(
        models=SimpleNamespace(generate_content=fake_generate),
        caches=SimpleNamespace(create=lambda **_kw: SimpleNamespace(name="cache")),
    )

    patch_targets = [f"{module_path}.make_client"]
    if func_name == "_llm_match_lines":
        patch_targets.append(
            "invoice_processing.shared_libraries.genai_client.make_client"
        )

    with ExitStack() as stack:
        for target in patch_targets:
            stack.enter_context(
                patch(target, lambda *a, **kw: fake_client, create=True)
            )
        getattr(mod, func_name)(**call_kwargs)

    assert captured["config"].thinking_config is not None
    assert captured["config"].thinking_config.thinking_budget == 0
