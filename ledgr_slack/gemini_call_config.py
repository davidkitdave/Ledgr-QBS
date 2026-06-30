"""Shared GenerateContentConfig defaults for Ledgr Gemini calls."""

from __future__ import annotations

from google.genai import types

DEFAULT_THINKING_BUDGET = 0
DEFAULT_MAX_OUTPUT_TOKENS = 65536
ABSTAIN_BOUNDARY_THINKING_BUDGET = 1024


def default_llm_config(**overrides: object) -> types.GenerateContentConfig:
    """Easy-path config: structured output with thinking disabled."""
    thinking = overrides.pop("thinking_config", None)
    if thinking is None:
        thinking = types.ThinkingConfig(thinking_budget=DEFAULT_THINKING_BUDGET)
    overrides.setdefault("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    return types.GenerateContentConfig(thinking_config=thinking, **overrides)


def abstain_boundary_llm_config(**overrides: object) -> types.GenerateContentConfig:
    """Bounded thinking for flagged abstain-boundary audit/reasoning calls only."""
    thinking = overrides.pop("thinking_config", None)
    if thinking is None:
        thinking = types.ThinkingConfig(
            thinking_budget=ABSTAIN_BOUNDARY_THINKING_BUDGET,
        )
    return types.GenerateContentConfig(thinking_config=thinking, **overrides)
