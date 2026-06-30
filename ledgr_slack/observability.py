"""Sentry init for the Slack serving path."""

from __future__ import annotations

import os

import sentry_sdk

_initialized = False


def init_sentry_if_configured() -> None:
    """Lazy-init Sentry when ``SENTRY_DSN`` is set; otherwise no-op."""
    global _initialized
    if _initialized:
        return
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        _initialized = True
        return
    environment = os.environ.get("SENTRY_ENVIRONMENT", "").strip() or None
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        traces_sample_rate=0.0,
    )
    _initialized = True

