"""google-genai client factory for ledgr_agent."""

from __future__ import annotations

import os
from typing import Optional

from google import genai
from google.genai import types

from ledgr_agent.shared.model_config import lite_model, read_model, std_model


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


__all__ = ["lite_model", "make_client", "read_model", "std_model"]
