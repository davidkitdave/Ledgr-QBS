"""Shared google-genai client factory — env-driven backend (AI Studio vs Vertex).

GOOGLE_GENAI_USE_VERTEXAI=FALSE -> Gemini Developer API (AI Studio) via GOOGLE_API_KEY.
GOOGLE_GENAI_USE_VERTEXAI=TRUE  -> Vertex AI (asia-southeast1) via ADC + project/location.
Keeps dev on AI Studio (no Vertex quota) while prod can stay in-region for PDPA.

Model names: import from :mod:`model_config` (single source of truth).
"""
from __future__ import annotations

import os
from typing import Optional

from google import genai
from google.genai import types

from .model_config import (  # noqa: F401 — re-export for callers
    chat_model,
    default_model,
    lite_model,
    model_for,
    read_model,
    std_model,
)


def _use_vertex() -> bool:
    return os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE").strip().upper() in ("TRUE", "1", "YES")


def retry_http_options() -> types.HttpOptions:
    return types.HttpOptions(retry_options=types.HttpRetryOptions(initial_delay=1, attempts=3))


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
