"""Process-local Slack event dedup + file_shared futures."""

from __future__ import annotations

import asyncio

from app.slack_app import _SeenEvents

_seen = _SeenEvents()
_file_futures: dict[str, asyncio.Future] = {}
