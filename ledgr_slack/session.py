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
) -> dict[str, Any]:
    """Build the ``state_delta`` passed to ``Runner.run_async`` for a file drop."""
    return {
        "channel_id": channel_id,
        "file_id": file_id,
        "source_filename": source_filename,
        "temp:artifact_name": artifact_name,
        "input_page_count": input_page_count,
        **profile_delta,
    }
