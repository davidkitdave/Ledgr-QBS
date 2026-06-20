"""Ledgr chat-lane ReflectAndRetry plugin.

Extends ADK's ``ReflectAndRetryToolPlugin`` so tool results that return JSON
with ``"status": "error"`` or ``"status": "not_found"`` trigger a retry with
reflection guidance — useful when the model picks the wrong diagnostic tool
or queries before the ledger is loaded.

See ADR-0013 and plan P7.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from google.adk.plugins.reflect_retry_tool_plugin import ReflectAndRetryToolPlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

_RETRYABLE_STATUSES = frozenset({"error", "not_found"})


class LedgrReflectRetryPlugin(ReflectAndRetryToolPlugin):
    """Retry when a tool returns a structured error/not_found payload."""

    def __init__(self, *, max_retries: int = 2) -> None:
        super().__init__(max_retries=max_retries, name="ledgr_reflect_retry")

    async def extract_error_from_result(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: Any,
    ) -> Optional[dict[str, Any]]:
        payload = _coerce_status_payload(result)
        if payload is None:
            return None
        status = str(payload.get("status") or "").lower()
        if status in _RETRYABLE_STATUSES:
            return payload
        return None


def _coerce_status_payload(result: Any) -> dict[str, Any] | None:
    """Parse tool return value into a dict when it carries a ``status`` field."""
    if isinstance(result, dict):
        return result if "status" in result else None
    if isinstance(result, str):
        text = result.strip()
        if not text.startswith("{"):
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) and "status" in parsed else None
    return None
