"""Compatibility helper for native Slack Block Kit primitives.

Controls whether builders emit native Block Kit blocks (plan, card, carousel,
data_table, context_actions, …) or fall back to section+actions.

Environment variable: LEDGR_NATIVE_BLOCKS
  "1" / "true" / "yes"  → always use native blocks
  "0" / "false" / "no"  → always fall back to section+actions
  "auto" (default)       → use cached per-channel probe result; default True
  (unset)                → same as "auto"

The cache is populated by record_probe_result() — call it from a smoke-script
or runtime probe; the helper itself never makes network calls.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NATIVE_BLOCK_TYPES = (
    "plan",
    "card",
    "carousel",
    "data_table",
    "context_actions",
    "task_card",
    "alert",
)

# ---------------------------------------------------------------------------
# Internal cache  {channel_id: supported}
# ---------------------------------------------------------------------------

_PROBE_CACHE: dict[str, bool] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def supports_native_blocks(channel_id: str | None = None) -> bool:
    """Return True if native Block Kit primitives should be used.

    Reads LEDGR_NATIVE_BLOCKS:
      "1"/"true"/"yes"  → always True
      "0"/"false"/"no"  → always False
      "auto" or unset   → cached probe result for channel_id, else True
    """
    raw = os.environ.get("LEDGR_NATIVE_BLOCKS", "auto").strip().lower()

    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False

    # "auto" or any unrecognised value → consult cache
    if channel_id is not None and channel_id in _PROBE_CACHE:
        return _PROBE_CACHE[channel_id]

    # No cached result — assume supported
    return True


def record_probe_result(channel_id: str, supported: bool) -> None:
    """Store a runtime probe result so supports_native_blocks() can return it.

    Intended to be called by a smoke-script or probe utility, not from within
    the helper itself.
    """
    _PROBE_CACHE[channel_id] = supported


def _reset_for_tests() -> None:
    """Clear the in-memory cache.  Call this between tests."""
    _PROBE_CACHE.clear()
