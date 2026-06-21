"""Shared GenerateContentConfig defaults for Ledgr Gemini calls.

WS-6.3: disable thinking on the easy path (extract, classify, default COA).
Spec §10 — thinking bills as output with no documented gain for classification-style picks.
"""
from __future__ import annotations

from google.genai import types

# 0 = disabled per Gemini ThinkingConfig docs.
DEFAULT_THINKING_BUDGET = 0

# Reserved for optional abstain-boundary audit calls (spec §10); not wired until that path exists.
ABSTAIN_BOUNDARY_THINKING_BUDGET = 1024


def default_llm_config(**overrides: object) -> types.GenerateContentConfig:
    """Easy-path config: structured output with thinking disabled."""
    thinking = overrides.pop("thinking_config", None)
    if thinking is None:
        thinking = types.ThinkingConfig(thinking_budget=DEFAULT_THINKING_BUDGET)
    return types.GenerateContentConfig(thinking_config=thinking, **overrides)


def abstain_boundary_llm_config(**overrides: object) -> types.GenerateContentConfig:
    """Bounded thinking for flagged abstain-boundary audit/reasoning calls only."""
    thinking = overrides.pop("thinking_config", None)
    if thinking is None:
        thinking = types.ThinkingConfig(
            thinking_budget=ABSTAIN_BOUNDARY_THINKING_BUDGET,
        )
    return types.GenerateContentConfig(thinking_config=thinking, **overrides)
