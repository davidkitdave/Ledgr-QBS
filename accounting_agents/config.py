"""Ledgr accounting_agents configuration.

Env loading and model-tier constants for the ADK 2.0 orchestration layer.

Design rules:
- No network calls or heavy imports at module import time (import-safe).
- AI Studio (Gemini Developer API) is the only backend — never call
  google.auth.default(); never force Vertex.
- All values read from environment at call time via lazy getters; module-level
  constants are resolved once on first import but rely only on os.environ.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# .env loading  (matches invoice_processing/core/config.py + app/config.py style)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent  # repo root

try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass  # dotenv not installed; assume env vars are set externally

# ---------------------------------------------------------------------------
# Force AI Studio — must happen before any google-genai/ADK import resolves
# the backend.  Only set if the caller hasn't already set it; never override
# an explicit TRUE from the outside (so integration tests can toggle).
# ---------------------------------------------------------------------------

if "GOOGLE_GENAI_USE_VERTEXAI" not in os.environ:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"

from invoice_processing.shared_libraries.model_config import (  # noqa: E402,F401
    MODEL_CHAT,
    MODEL_LITE,
    MODEL_READ,
    MODEL_STD,
    model_for,
    resolve_model as _resolve_model,
)

# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _env_prefix() -> str:
    """Return "[dev] " in dev/unset environments, "" in prod.

    Prepend to bot status message text so it's immediately obvious which
    Slack app replied. Mirrors the LEDGR_NATIVE_BLOCKS pattern from
    app/native_blocks_compat.py — plain env-var helper, no config class.
    """
    env = (os.environ.get("LEDGR_ENV") or "dev").strip().lower()
    return "[dev] " if env == "dev" else ""


def is_playground_seed_enabled() -> bool:
    """Return True when the dev playground default-profile seed should activate.

    The seed injects a synthetic ``ClientContext`` into session state when no
    real channel/client profile resolves (e.g. ``adk web`` / agents-cli playground).
    It is NEVER active in production.

    Rules (checked in priority order):
    1. ``LEDGR_PLAYGROUND_SEED=false`` → always off (explicit opt-out).
    2. ``LEDGR_ENV=prod`` → always off (production guard).
    3. ``LEDGR_PLAYGROUND_SEED=true`` → always on.
    4. Default (env unset or "dev") → on.
    """
    seed_flag = os.environ.get("LEDGR_PLAYGROUND_SEED", "").strip().lower()
    if seed_flag == "false":
        return False
    env = (os.environ.get("LEDGR_ENV") or "dev").strip().lower()
    if env == "prod":
        return False
    if seed_flag == "true":
        return True
    # Default: enabled in dev/unset environments.
    return True


# ---------------------------------------------------------------------------
# Firestore namespace helper
# ---------------------------------------------------------------------------


def _ns(name: str) -> str:
    """Apply the optional Firestore namespace prefix to a root collection name.

    When ``LEDGR_FIRESTORE_NAMESPACE`` is set (e.g. "dev"), collection
    ``"clients"`` becomes ``"dev_clients"`` so dev and prod can share a GCP
    project without data crossover.  When the var is unset (the recommended
    prod path is a separate Firestore project), the name is returned unchanged.
    Apply only to TOP-LEVEL collection names; subcollections (coa, entity_memory)
    are not affected.
    """
    prefix = os.environ.get("LEDGR_FIRESTORE_NAMESPACE", "").strip()
    return f"{prefix}_{name}" if prefix else name


# ---------------------------------------------------------------------------
# Model tier constants — defined in invoice_processing.shared_libraries.model_config
# Configure via LEDGR_MODEL_* in .env (see .env.example).
# ---------------------------------------------------------------------------
