"""Shared GenerateContentConfig defaults for Ledgr Gemini calls.

WS-6.3: disable thinking on the easy path (extract, classify, default COA).
Spec §10 — thinking bills as output with no documented gain for classification-style picks.
"""
from __future__ import annotations

from google.genai import types

# 0 = disabled per Gemini ThinkingConfig docs.
DEFAULT_THINKING_BUDGET = 0

# Gemini 2.5 Flash-Lite documented output ceiling. Setting this explicitly
# is what stops the silent truncation that previously motivated pre-chunking.
DEFAULT_MAX_OUTPUT_TOKENS = 65536

# Reserved for optional abstain-boundary audit calls (spec §10); not wired until that path exists.
ABSTAIN_BOUNDARY_THINKING_BUDGET = 1024


def default_llm_config(**overrides: object) -> types.GenerateContentConfig:
    """Easy-path config: structured output with thinking disabled.

    ``max_output_tokens`` is pinned to Gemini 2.5 Flash-Lite's documented
    ceiling (65536) so a single call can hold the full structured JSON for any
    realistic invoice or multi-receipt PDF — including the 35-page, ~20MB
    multi-receipt case (issue #16). Without this, the SDK default was too low
    and silently truncated output, which is what originally motivated the
    pre-chunker at ``pdf_chunks.should_chunk_pdf``. Per the Phase-0 control
    experiment on ``feat/minimal-extract-control-experiment``: 1 un-chunked
    call with this budget produces all 852 lines; the chunked path produced
    680 and dropped into ``needs_review``.
    """
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
