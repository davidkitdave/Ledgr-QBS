"""Single source of truth for Ledgr Gemini model names.

Configure models in ``.env`` (see ``.env.example``):

  LEDGR_MODEL_LITE  — invoices, classification, digital bank PDFs (pdfplumber)
  LEDGR_MODEL_STD   — scanned bank statements, default multimodal / chat
  LEDGR_MODEL_CHAT  — chat-lane root agent (optional; defaults to STD)
  LEDGR_MODEL_READ  — Understand / Phase-1 invoice read (optional; defaults to LITE)

Legacy: ``GEMINI_FLASH_MODEL`` is honored only when ``LEDGR_MODEL_STD`` is unset.
"""

from __future__ import annotations

import os

_DEFAULT_LITE = "gemini-2.5-flash-lite"
_DEFAULT_STD = "gemini-2.5-flash"


def resolve_model(tier: str) -> str:
    """Return the model id for ``tier`` (``lite`` or ``std``)."""
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
    """Lighter tier: invoices, classification, digital bank PDFs."""
    return resolve_model("lite")


def std_model() -> str:
    """Stronger tier: scanned bank statements, chat orchestration."""
    return resolve_model("std")


def chat_model() -> str:
    """Chat-lane root agent (22-tool orchestration)."""
    return os.environ.get("LEDGR_MODEL_CHAT") or std_model()


def read_model() -> str:
    """Understand / Phase-1 faithful invoice read."""
    return os.environ.get("LEDGR_MODEL_READ") or lite_model()


def model_for(complexity: str) -> str:
    """Map a complexity label to a model id (``lite`` → lite tier, else std)."""
    return lite_model() if complexity.strip().lower() == "lite" else std_model()


def default_model() -> str:
    """Backward-compatible alias for :func:`std_model`."""
    return std_model()


# Import-time snapshots for ADK ``LlmAgent(model=...)`` definitions.
MODEL_LITE: str = resolve_model("lite")
MODEL_STD: str = resolve_model("std")
MODEL_CHAT: str = chat_model()
MODEL_READ: str = read_model()
