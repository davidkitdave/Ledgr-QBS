"""Tests for socket-mode event deduplication in accounting_agents.slack_runner.

Verifies that a re-delivered Slack event (same event_id) does NOT trigger a
second side-effect for member_joined_channel, file_shared, or message events.

Strategy: extract the registered Bolt handler by monkey-patching AsyncApp, then
call it directly via asyncio.run(), asserting the downstream fn is called once.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.slack_app import _SeenEvents


# ---------------------------------------------------------------------------
# _SeenEvents unit tests (the shared primitive)
# ---------------------------------------------------------------------------


def test_seen_events_first_call_returns_false():
    s = _SeenEvents()
    assert s.seen_before("ev-1") is False


def test_seen_events_second_call_returns_true():
    s = _SeenEvents()
    s.seen_before("ev-1")
    assert s.seen_before("ev-1") is True


def test_seen_events_different_ids_are_independent():
    s = _SeenEvents()
    s.seen_before("ev-1")
    assert s.seen_before("ev-2") is False


def test_seen_events_evicts_oldest_when_full():
    s = _SeenEvents(cap=3)
    s.seen_before("a")
    s.seen_before("b")
    s.seen_before("c")
    # "d" triggers eviction of "a"
    s.seen_before("d")
    # "a" was evicted → treated as new
    assert s.seen_before("a") is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_body(event_id: str, event_type: str = "member_joined_channel") -> dict:
    return {
        "event_id": event_id,
        "event": {"type": event_type, "ts": "1234567890.000001"},
    }


def _make_event(event_type: str = "member_joined_channel", ts: str = "1234567890.000001") -> dict:
    return {"type": event_type, "ts": ts}


def _capture_handlers():
    """Return a fake AsyncApp whose .event() decorator captures registered handlers."""
    registered = {}
    fake_app = MagicMock()

    def event_decorator(name):
        def decorator(fn):
            registered[name] = fn
            return fn
        return decorator

    fake_app.event = event_decorator
    fake_app.action = lambda *a, **k: (lambda fn: fn)
    fake_app.view = lambda *a, **k: (lambda fn: fn)
    fake_app.command = lambda *a, **k: (lambda fn: fn)
    return fake_app, registered


# ---------------------------------------------------------------------------
# member_joined_channel dedup
# ---------------------------------------------------------------------------


def test_member_joined_dedup_calls_handler_once():
    """Same event_id delivered twice → handle_member_joined invoked only once."""
    from accounting_agents import slack_runner

    fresh_seen = _SeenEvents()
    body = _make_body("Ev001", "member_joined_channel")
    event = _make_event("member_joined_channel")
    fake_context = {"bot_user_id": "B123"}
    fake_client = MagicMock()

    fake_app, registered = _capture_handlers()
    mock_thread = AsyncMock()

    with patch.object(slack_runner, "_seen", fresh_seen), \
         patch("asyncio.to_thread", mock_thread), \
         patch("slack_bolt.async_app.AsyncApp", return_value=fake_app), \
         patch("invoice_processing.export.client_context.InMemoryClientStore"):

        runner_mock = MagicMock()
        runner_mock.app_name = "acc"
        slack_runner.build_async_app(
            runner=runner_mock,
            ledger_store=MagicMock(),
            db=MagicMock(),
        )

        handler = registered["member_joined_channel"]

        # First delivery — asyncio.to_thread wraps handle_member_joined.
        asyncio.run(handler(event=event, body=body, context=fake_context, client=fake_client))
        assert mock_thread.call_count == 1

        # Duplicate delivery — must be a no-op.
        asyncio.run(handler(event=event, body=body, context=fake_context, client=fake_client))
        assert mock_thread.call_count == 1  # unchanged


# ---------------------------------------------------------------------------
# file_shared dedup
# ---------------------------------------------------------------------------


def test_file_shared_dedup_does_not_call_process():
    """Same file_shared event_id delivered twice → process_file_event is NEVER called.

    After Phase 1A (file_shared race fix), ``file_shared`` is no longer the
    document owner — the message/file_share handler is. So duplicate
    file_shared events should both be no-ops, and process_file_event should
    not be invoked from this path at all. Dedup is still enforced via the
    ``file:{id}`` guard for the COA ingest path.
    """
    from accounting_agents import slack_runner

    fresh_seen = _SeenEvents()
    body = _make_body("Ev002", "file_shared")
    event = {
        "type": "file_shared",
        "file_id": "F_abc",
        "channel_id": "C_test",
        "event_ts": "1234567890.000002",
    }
    fake_client = MagicMock()

    fake_app, registered = _capture_handlers()
    mock_pfe = AsyncMock(return_value={"status": "delivered"})

    with patch.object(slack_runner, "_seen", fresh_seen), \
         patch("accounting_agents.slack_runner.process_file_event", mock_pfe), \
         patch("slack_bolt.async_app.AsyncApp", return_value=fake_app), \
         patch("invoice_processing.export.client_context.InMemoryClientStore"):

        runner_mock = MagicMock()
        runner_mock.app_name = "acc"
        slack_runner.build_async_app(
            runner=runner_mock,
            ledger_store=MagicMock(),
            db=MagicMock(),
        )

        handler = registered["file_shared"]

        # First delivery — must NOT call process_file_event (message handler owns it).
        asyncio.run(handler(event=event, body=body, client=fake_client))
        assert mock_pfe.call_count == 0

        # Duplicate delivery — also no-op.
        asyncio.run(handler(event=event, body=body, client=fake_client))
        assert mock_pfe.call_count == 0  # still 0


# ---------------------------------------------------------------------------
# message dedup
# ---------------------------------------------------------------------------


def test_message_dedup_calls_answer_once():
    """Same message event_id delivered twice → answer_question called only once."""
    from accounting_agents import slack_runner

    fresh_seen = _SeenEvents()
    body = _make_body("Ev003", "message")
    event = {
        "type": "message",
        "ts": "1234567890.000003",
        "channel": "C_test",
        "text": "What is my balance?",
    }
    fake_client = MagicMock()

    fake_app, registered = _capture_handlers()
    mock_aq = AsyncMock(return_value={"status": "answered", "text": "x"})

    with patch.object(slack_runner, "_seen", fresh_seen), \
         patch("accounting_agents.slack_runner.answer_question", mock_aq), \
         patch("slack_bolt.async_app.AsyncApp", return_value=fake_app), \
         patch("invoice_processing.export.client_context.InMemoryClientStore"):

        runner_mock = MagicMock()
        runner_mock.app_name = "acc"
        slack_runner.build_async_app(
            runner=runner_mock,
            ledger_store=MagicMock(),
            db=MagicMock(),
        )

        handler = registered["message"]

        # First delivery.
        asyncio.run(handler(event=event, body=body, client=fake_client))
        assert mock_aq.call_count == 1

        # Duplicate delivery.
        asyncio.run(handler(event=event, body=body, client=fake_client))
        assert mock_aq.call_count == 1  # still 1
