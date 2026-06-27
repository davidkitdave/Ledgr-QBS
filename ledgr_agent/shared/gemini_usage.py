"""Extract token usage metadata from a Gemini generate_content response."""

from __future__ import annotations

from typing import Any


def usage_from_response(resp: object) -> dict[str, Any]:
    meta = getattr(resp, "usage_metadata", None) or getattr(resp, "usage", None)
    if meta is None:
        return {}
    out: dict[str, Any] = {}
    for attr in (
        "prompt_token_count",
        "candidates_token_count",
        "thoughts_token_count",
        "total_token_count",
        "cached_content_token_count",
    ):
        val = getattr(meta, attr, None)
        if val is not None:
            out[attr] = val
    return out
