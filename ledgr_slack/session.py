"""Firestore client profile → ADK session state for Slack runs."""

from __future__ import annotations

from typing import Any


def profile_state_delta(client_store: Any, channel_id: str) -> dict[str, Any]:
    """Return client profile keys to seed the ledgr_agent session."""
    ctx = client_store.get_by_channel(channel_id)
    if ctx is None:
        return {}
    delta = ctx.to_state()
    from accounting_agents.credit_delivery import resolve_firm_id_from_client

    resolved_firm = resolve_firm_id_from_client(ctx)
    if resolved_firm:
        delta["firm_id"] = resolved_firm
    return delta


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
