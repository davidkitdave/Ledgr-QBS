"""Firestore client profile → ADK session state for Slack runs."""

from __future__ import annotations

from typing import Any


def run_state_delta(
    *,
    channel_id: str,
    file_id: str,
    source_filename: str,
    artifact_name: str,
    profile_delta: dict[str, Any],
    input_page_count: int = 1,
    defer_slack_delivery: bool = False,
) -> dict[str, Any]:
    """Build the ``state_delta`` passed to ``Runner.run_async`` for a file drop."""
    return {
        "channel_id": channel_id,
        "file_id": file_id,
        "source_filename": source_filename,
        "temp:artifact_name": artifact_name,
        "input_page_count": input_page_count,
        "defer_slack_delivery": defer_slack_delivery,
        # Slack owns the single delivery charge + coin footer (not build_sheets).
        "charge_at_slack_delivery": True,
        **profile_delta,
    }
