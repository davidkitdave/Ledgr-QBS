"""GenerateContentConfig defaults for ledgr_agent Gemini calls."""

from __future__ import annotations

from google.genai import types

DEFAULT_THINKING_BUDGET = 0
DEFAULT_MAX_OUTPUT_TOKENS = 65536


def default_llm_config(**overrides: object) -> types.GenerateContentConfig:
    thinking = overrides.pop("thinking_config", None)
    if thinking is None:
        thinking = types.ThinkingConfig(thinking_budget=DEFAULT_THINKING_BUDGET)
    overrides.setdefault("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    return types.GenerateContentConfig(thinking_config=thinking, **overrides)
