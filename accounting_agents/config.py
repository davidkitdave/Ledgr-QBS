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
# Model tier constants (env-overridable)
# ---------------------------------------------------------------------------

#: Lighter model for simpler documents: invoices, receipts, coordinator routing.
MODEL_LITE: str = os.environ.get("LEDGR_MODEL_LITE", "gemini-2.5-flash-lite")

#: Stronger model for complex documents: bank statements.
MODEL_STD: str = os.environ.get("LEDGR_MODEL_STD", "gemini-2.5-flash")


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
