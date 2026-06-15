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


def _resolve_model(tier: str) -> str:
    """Return the model name for ``tier`` ('lite' or 'std').

    Resolution order:
    1. ``LEDGR_MODEL_<TIER>`` env override — allows flipping per env without
       code changes (e.g. set LEDGR_MODEL_LITE in a Cloud Run secret when
       gemini-2.5-flash-lite reaches asia-southeast1).
    2. Per-env default:
       - dev / unset  → lite=gemini-2.5-flash-lite, std=gemini-2.5-flash
                        (AI Studio, US/global availability)
       - prod         → both=gemini-2.5-flash
                        (Vertex asia-southeast1; flash-lite not yet available there)
    """
    override = os.environ.get(f"LEDGR_MODEL_{tier.upper()}")
    if override:
        return override
    env = (os.environ.get("LEDGR_ENV") or "dev").strip().lower()
    if env == "prod":
        return "gemini-2.5-flash"
    return "gemini-2.5-flash-lite" if tier == "lite" else "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Model tier constants (env-overridable)
# ---------------------------------------------------------------------------

#: Lighter model for simpler documents: invoices, receipts, coordinator routing.
MODEL_LITE: str = _resolve_model("lite")

#: Stronger model for complex documents: bank statements.
MODEL_STD: str = _resolve_model("std")


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
# Helper
# ---------------------------------------------------------------------------

def model_for(complexity: str) -> str:
    """Return the model name for the given complexity tier.

    Args:
        complexity: ``"lite"`` for simpler documents (invoices, routing),
                    ``"std"`` or anything else for complex documents (bank
                    statements).

    Returns:
        The model string suitable for passing to ``LlmAgent(model=...)``.
    """
    if complexity.lower() == "lite":
        return MODEL_LITE
    return MODEL_STD
