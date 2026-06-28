"""Gemini client, model config, MIME, and page counting for ledgr_agent."""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any, Optional

from google import genai
from google.genai import types

_DEFAULT_LITE = "gemini-2.5-flash-lite"
_DEFAULT_STD = "gemini-2.5-flash"

_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tiff": "image/tiff",
}

DEFAULT_THINKING_BUDGET = 0
DEFAULT_MAX_OUTPUT_TOKENS = 65536


def resolve_model(tier: str) -> str:
    key = tier.strip().lower()
    override = os.environ.get(f"LEDGR_MODEL_{key.upper()}")
    if override:
        return override
    if key == "lite":
        return _DEFAULT_LITE
    if key == "std":
        return os.environ.get("GEMINI_FLASH_MODEL", _DEFAULT_STD)
    raise ValueError(f"unknown model tier: {tier!r} (expected 'lite' or 'std')")


def lite_model() -> str:
    return resolve_model("lite")


def std_model() -> str:
    return resolve_model("std")


def read_model() -> str:
    return os.environ.get("LEDGR_MODEL_READ") or lite_model()


def _use_vertex() -> bool:
    return os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE").strip().upper() in ("TRUE", "1", "YES")


def retry_http_options() -> types.HttpOptions:
    return types.HttpOptions(retry_options=types.HttpRetryOptions(initial_delay=1, attempts=5))


def make_client(project: Optional[str] = None, location: Optional[str] = None) -> genai.Client:
    http_options = retry_http_options()
    if _use_vertex():
        return genai.Client(
            vertexai=True,
            project=project or os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT"),
            location=location or os.getenv("LOCATION", "asia-southeast1"),
            http_options=http_options,
        )
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_GENAI_USE_VERTEXAI is FALSE but no GOOGLE_API_KEY/GEMINI_API_KEY set")
    return genai.Client(api_key=api_key, http_options=http_options)


def default_llm_config(**overrides: object) -> types.GenerateContentConfig:
    thinking = overrides.pop("thinking_config", None)
    if thinking is None:
        thinking = types.ThinkingConfig(thinking_budget=DEFAULT_THINKING_BUDGET)
    overrides.setdefault("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    return types.GenerateContentConfig(thinking_config=thinking, **overrides)


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


def mime_for(path: str | Path) -> str:
    return _MIME_BY_EXT.get(Path(path).suffix.lower(), "application/octet-stream")


def count_input_pages(data: bytes, mime_type: str) -> int:
    if mime_type != "application/pdf":
        return 1
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return len(pdf.pages) or 1
    except Exception:
        return 1


__all__ = [
    "count_input_pages",
    "default_llm_config",
    "lite_model",
    "make_client",
    "mime_for",
    "read_model",
    "resolve_model",
    "std_model",
    "usage_from_response",
]
