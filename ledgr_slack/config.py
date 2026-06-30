"""Ledgr Slack runtime configuration (env loading, models, Firestore namespace)."""

from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass

if "GOOGLE_GENAI_USE_VERTEXAI" not in os.environ:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"

from ledgr_slack.model_config import (  # noqa: E402
    MODEL_CHAT,
    MODEL_LITE,
    MODEL_READ,
    MODEL_STD,
    model_for,
    resolve_model,
)

_resolve_model = resolve_model

__all__ = [
    "MODEL_CHAT",
    "MODEL_LITE",
    "MODEL_READ",
    "MODEL_STD",
    "_env_prefix",
    "_ns",
    "is_playground_seed_enabled",
    "model_for",
    "resolve_model",
]


def _env_prefix() -> str:
    """Return ``[dev] `` in dev/unset environments, ``""`` in prod."""
    env = (os.environ.get("LEDGR_ENV") or "dev").strip().lower()
    return "[dev] " if env == "dev" else ""


def _ns(name: str) -> str:
    """Apply optional ``LEDGR_FIRESTORE_NAMESPACE`` prefix to a collection name."""
    prefix = os.environ.get("LEDGR_FIRESTORE_NAMESPACE", "").strip()
    return f"{prefix}_{name}" if prefix else name


def is_playground_seed_enabled() -> bool:
    """Return True when the dev playground default-profile seed should activate."""
    seed_flag = os.environ.get("LEDGR_PLAYGROUND_SEED", "").strip().lower()
    if seed_flag == "false":
        return False
    env = (os.environ.get("LEDGR_ENV") or "dev").strip().lower()
    if env == "prod":
        return False
    if seed_flag == "true":
        return True
    return True
