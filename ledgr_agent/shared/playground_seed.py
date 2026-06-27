"""Minimal playground profile seeding for ledgr_agent sessions."""

from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)


def _seed_enabled() -> bool:
    if (os.environ.get("LEDGR_ENV") or "dev").strip().lower() == "prod":
        return False
    raw = os.environ.get("LEDGR_PLAYGROUND_SEED", "")
    return raw.strip().lower() not in {"false", "0", "no", "off"}


def seed_playground_profile_if_needed(state: dict) -> bool:
    """Inject a lightweight playground profile into session state when empty."""
    if state is None:
        return False
    if state.get("client_id") is not None or state.get("client_name") is not None:
        return False
    if not _seed_enabled():
        return False

    state.update(
        {
            "client_id": os.environ.get("LEDGR_PLAYGROUND_CLIENT_ID", "playground"),
            "client_name": os.environ.get("LEDGR_PLAYGROUND_CLIENT_NAME", "Playground Client"),
            "region": os.environ.get("LEDGR_PLAYGROUND_REGION", "SINGAPORE"),
            "software": os.environ.get("LEDGR_PLAYGROUND_SOFTWARE", "qbs"),
            "base_currency": os.environ.get("LEDGR_PLAYGROUND_CURRENCY", "SGD"),
            "tax_registered": True,
            "fye_month": 12,
            "ledger_data": [],
            "ledger_row_count": 0,
            "fy_loaded": "none",
            "fy_pointers": [],
            "processing_log": [],
            "pending_reviews": [],
        }
    )
    _log.info(
        "ledgr_agent: playground profile seeded (client_id=%s)",
        state.get("client_id"),
    )
    return True
