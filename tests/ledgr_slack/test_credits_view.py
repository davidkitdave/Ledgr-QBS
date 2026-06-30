"""Tests for Slack credit visibility Block Kit builders."""

from __future__ import annotations

from ledgr_agent.billing import CreditService, InMemoryCreditStore, configure_shared_credit_service
from ledgr_slack.credit_adapter import format_credit_footer
from ledgr_slack.credits_view import credits_ephemeral_blocks, format_coin_footer


def test_coin_footer_format() -> None:
    assert "🪙" in format_coin_footer(credits_used=1, credits_remaining=99)
    assert "🪙" in format_credit_footer(credits_used=2, credits_remaining=8)


def test_credits_ephemeral_master_view() -> None:
    service = CreditService(InMemoryCreditStore())
    service.grant("T1", 100, note="test")
    configure_shared_credit_service(service)
    blocks = credits_ephemeral_blocks(firm_id="T1", channel_id=None)
    text = blocks[0]["text"]["text"]
    assert "🪙" in text
    assert "100" in text
    assert "T1" in text


def test_credits_ephemeral_channel_view() -> None:
    service = CreditService(InMemoryCreditStore())
    service.grant("T1", 50, note="test")
    configure_shared_credit_service(service)
    blocks = credits_ephemeral_blocks(
        firm_id="T1",
        channel_id="C1",
        channel_name="qa-channel",
    )
    text = blocks[0]["text"]["text"]
    assert "qa-channel" in text
    assert "50" in text
