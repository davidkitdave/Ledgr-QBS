"""Gemini model name helpers for ledgr_agent."""

from __future__ import annotations

import os

_DEFAULT_LITE = "gemini-2.5-flash-lite"
_DEFAULT_STD = "gemini-2.5-flash"


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
