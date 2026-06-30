"""App Home tab — firm credit balance and per-channel usage."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ledgr_slack.credits_view import app_home_view

logger = logging.getLogger(__name__)


async def _channel_name_map(slack_client: Any, channel_ids: list[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for channel_id in channel_ids:
        if not channel_id:
            continue
        try:
            resp = await asyncio.to_thread(
                slack_client.conversations_info, channel=channel_id
            )
            data = resp.data if hasattr(resp, "data") else resp
            channel = data.get("channel") if isinstance(data, dict) else None
            name = channel.get("name") if isinstance(channel, dict) else None
            if name:
                names[channel_id] = str(name)
        except Exception:  # noqa: BLE001
            logger.debug("conversations_info failed for %s", channel_id, exc_info=True)
    return names


async def publish_app_home(
    *,
    slack_client: Any,
    user_id: str,
    firm_id: str,
) -> None:
    from ledgr_slack.credits_report import usage_by_channel

    by_channel = usage_by_channel(firm_id)
    names = await _channel_name_map(slack_client, list(by_channel.keys()))
    view = app_home_view(firm_id=firm_id, channel_names=names)
    await asyncio.to_thread(
        slack_client.views_publish,
        user_id=user_id,
        view=view,
    )

