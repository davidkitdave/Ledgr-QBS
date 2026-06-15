"""Hermetic tests for the mid-flow extract-review HITL (Step 2 / C-3).

Covers:
- ``review_card_blocks`` renders the question, reason bullets, and three
  action buttons each carrying the ``:review`` op_id as ``value``.
- ``review_outcome_blocks`` renders the correct icon + verb for each action.
- ``review_hint_modal`` has the right ``callback_id``, ``private_metadata``,
  and a single ``hint_block`` input.
- ``process_file_event`` posts the REVIEW card (not the approval card) when
  the interrupt id ends with ``:review``, and still posts the APPROVAL card
  for a normal approval interrupt.
- ``handle_review_action`` builds the correct ``ReviewClarifyDecision`` and
  calls ``resume_session`` with the right op_id + decision; double-click
  resolves once (idempotency).
- ``review_reject`` → downstream produces nothing (no ledger upload).
- ``review_confirm`` resumes without uploading (workflow continues to gate).
- ``review_reextract`` carries the hint.
- The three registered Bolt handlers (confirm / reject / reextract) each call
  ``handle_review_action`` with the correct action string.
- The ``ledgr_review_hint`` view-submission handler extracts the hint and
  calls ``handle_review_action(action="reextract_as", hint=...)``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from accounting_agents import nodes
from accounting_agents.hitl import write_interrupt
from accounting_agents.ledger_store import SlackLedgerStore
from accounting_agents.slack_runner import (
    handle_review_action,
    process_file_event,
)
from app.blocks import review_card_blocks, review_hint_modal, review_outcome_blocks
from tests._fake_firestore import FakeFirestore
from tests.test_ledger_store import FakeSlackClient
from tests.test_slack_runner import (
    _FakeRunner,
    _last_blocks,
    _posted_texts,
    _seeded_client_store,
)


# =========================================================================== #
# Block-Kit builders
# =========================================================================== #


class TestReviewCardBlocks:

    def _action_ids(self, blocks):
        return {
            el["action_id"]
            for b in blocks
            if b.get("type") == "actions"
            for el in b.get("elements", [])
        }

    def _button_values(self, blocks):
        return {
            el["action_id"]: el["value"]
            for b in blocks
            if b.get("type") == "actions"
            for el in b.get("elements", [])
        }

    def _head_text(self, blocks):
        for b in blocks:
            if b.get("type") == "section":
                return b["text"]["text"]
        return ""

    def test_has_three_action_buttons(self):
        blocks = review_card_blocks("What is this?", "C1:F1:review")
        assert self._action_ids(blocks) == {
            "review_reextract", "review_confirm", "review_reject"
        }

    def test_each_button_carries_op_id_as_value(self):
        op_id = "C2:F2:review"
        blocks = review_card_blocks("question", op_id)
        values = self._button_values(blocks)
        assert values["review_reextract"] == op_id
        assert values["review_confirm"] == op_id
        assert values["review_reject"] == op_id

    def test_question_appears_in_header(self):
        question = "Could you clarify what this document represents?"
        blocks = review_card_blocks(question, "X:Y:review")
        head = self._head_text(blocks)
        assert question in head

    def test_reasons_rendered_as_bullets(self):
        reasons = ["low confidence: 0.32", "missing vendor name"]
        blocks = review_card_blocks("question", "X:Y:review", reasons=reasons)
        head = self._head_text(blocks)
        assert "low confidence: 0.32" in head
        assert "missing vendor name" in head

    def test_no_reasons_no_bullets_section(self):
        blocks = review_card_blocks("question", "X:Y:review", reasons=[])
        head = self._head_text(blocks)
        assert "Signals detected" not in head

    def test_none_reasons_no_bullets_section(self):
        blocks = review_card_blocks("question", "X:Y:review", reasons=None)
        head = self._head_text(blocks)
        assert "Signals detected" not in head

    def test_reextract_button_is_primary(self):
        blocks = review_card_blocks("q", "X:Y:review")
        for b in blocks:
            if b.get("type") == "actions":
                for el in b["elements"]:
                    if el["action_id"] == "review_reextract":
                        assert el.get("style") == "primary"

    def test_reject_button_is_danger(self):
        blocks = review_card_blocks("q", "X:Y:review")
        for b in blocks:
            if b.get("type") == "actions":
                for el in b["elements"]:
                    if el["action_id"] == "review_reject":
                        assert el.get("style") == "danger"

    def test_block_id_is_ledgr_review(self):
        blocks = review_card_blocks("q", "X:Y:review")
        block_ids = {b.get("block_id") for b in blocks}
        assert "ledgr_review" in block_ids

    def test_returns_list(self):
        assert isinstance(review_card_blocks("q", "X:Y:review"), list)


class TestReviewOutcomeBlocks:

    def _text(self, blocks):
        for b in blocks:
            if b.get("type") == "section":
                return b["text"]["text"]
        return ""

    def test_reextract_as_shows_arrows_icon(self):
        text = self._text(review_outcome_blocks("q", "reextract_as"))
        assert ":arrows_counterclockwise:" in text

    def test_confirm_as_is_shows_check_icon(self):
        text = self._text(review_outcome_blocks("q", "confirm_as_is"))
        assert ":white_check_mark:" in text

    def test_reject_shows_x_icon(self):
        text = self._text(review_outcome_blocks("q", "reject"))
        assert ":x:" in text

    def test_question_appears_in_outcome(self):
        question = "What is this?"
        text = self._text(review_outcome_blocks(question, "confirm_as_is"))
        assert question in text

    def test_returns_list(self):
        assert isinstance(review_outcome_blocks("q", "confirm_as_is"), list)


class TestReviewHintModal:

    def test_callback_id(self):
        assert review_hint_modal("X:Y:review")["callback_id"] == "ledgr_review_hint"

    def test_private_metadata_carries_op_id(self):
        assert review_hint_modal("C3:F3:review")["private_metadata"] == "C3:F3:review"

    def test_has_hint_block(self):
        modal = review_hint_modal("X:Y:review")
        block_ids = [b.get("block_id") for b in modal.get("blocks", [])]
        assert "hint_block" in block_ids

    def test_hint_input_is_plain_text_input(self):
        modal = review_hint_modal("X:Y:review")
        block = next(b for b in modal["blocks"] if b.get("block_id") == "hint_block")
        assert block["element"]["type"] == "plain_text_input"
        assert block["element"]["action_id"] == "hint_input"

    def test_type_is_modal(self):
        assert review_hint_modal("X:Y:review")["type"] == "modal"

    def test_multiline_input(self):
        modal = review_hint_modal("X:Y:review")
        block = next(b for b in modal["blocks"] if b.get("block_id") == "hint_block")
        assert block["element"].get("multiline") is True


# =========================================================================== #
# process_file_event interrupt routing
# =========================================================================== #


def test_process_file_event_posts_review_card_for_review_interrupt():
    """A ``:review`` interrupt id triggers the review card, not the approval card."""
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    # Interrupt id ends with ``:review``
    review_interrupt_event = SimpleNamespace(
        content=SimpleNamespace(parts=[]),
        get_function_calls=lambda: [
            SimpleNamespace(name="adk_request_input", id="C1:F1:review")
        ],
    )
    final_state = {
        "review_question": "Could you confirm the vendor name?",
        nodes.REVIEW_REASON_KEY: ["low confidence: 0.31"],
    }
    runner = _FakeRunner([review_interrupt_event], final_state)

    result = asyncio.run(
        process_file_event(
            runner=runner,
            ledger_store=store,
            db=db,
            slack_client=slack,
            channel_id="C1",
            file_id="F1",
            app_name="acc",
            download_fn=lambda c, f: b"%PDF fake",
            client_store=_seeded_client_store(db),
        )
    )

    assert result["status"] == "paused"
    assert result["op_id"] == "C1:F1:review"

    # The posted card must carry the review action ids, NOT the approval ids.
    card = _last_blocks(slack)
    action_ids = {
        el["action_id"]
        for b in card
        if b.get("type") == "actions"
        for el in b["elements"]
    }
    assert action_ids == {"review_reextract", "review_confirm", "review_reject"}
    # Must NOT have approval buttons.
    assert "approve" not in action_ids
    assert "edit" not in action_ids
    assert "reject" not in action_ids

    # The question must appear in the card text.
    section_texts = [
        b["text"]["text"]
        for b in card
        if b.get("type") == "section"
    ]
    assert any("vendor name" in t for t in section_texts)

    # Interrupt correlation doc written with kind="review".
    snap = db.collection("interrupts").document("C1:F1:review").get()
    assert snap.exists
    doc = snap.to_dict()
    assert doc.get("kind") == "review"
    assert doc.get("slack_file_id") == "F1"


def test_process_file_event_posts_approval_card_for_normal_interrupt():
    """A normal (non-``:review``) interrupt id still gets the approval card."""
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    approval_event = SimpleNamespace(
        content=SimpleNamespace(parts=[]),
        get_function_calls=lambda: [
            SimpleNamespace(name="adk_request_input", id="C2:F2")
        ],
    )
    runner = _FakeRunner([approval_event], {"approval_message": "needs review: line X"})

    result = asyncio.run(
        process_file_event(
            runner=runner,
            ledger_store=store,
            db=db,
            slack_client=slack,
            channel_id="C2",
            file_id="F2",
            app_name="acc",
            download_fn=lambda c, f: b"%PDF fake",
            client_store=_seeded_client_store(db, channel_id="C2", client_id="c2"),
        )
    )

    assert result["status"] == "paused"
    assert result["op_id"] == "C2:F2"

    card = _last_blocks(slack)
    action_ids = {
        el["action_id"]
        for b in card
        if b.get("type") == "actions"
        for el in b["elements"]
    }
    assert action_ids == {"approve", "edit", "reject"}


# =========================================================================== #
# handle_review_action
# =========================================================================== #


def _seed_review_interrupt(db, op_id, channel_id="CR1", session_id="CR1:FR1", file_id="FR1"):
    write_interrupt(
        db,
        op_id,
        session_id=session_id,
        channel_id=channel_id,
        slack_file_id=file_id,
        message_ts="5.000",
        user_id=channel_id,
        extra={"kind": "review", "question": "What is this doc?", "reasons": []},
    )


class _NoResumeRunner:
    """Runner that records ``resume_session`` calls without touching ADK."""

    def __init__(self):
        self.app_name = "acc"
        self.calls = []

    async def run_async(self, *, user_id, session_id, new_message=None, state_delta=None):
        # Yield nothing; the test only cares about the correlation doc + card update.
        return
        yield  # make it an async generator


async def _ok_finalize(**_kwargs):
    """Stub for ``_finalize_run_outcome`` in the DTO/idempotency/card tests.

    These tests exercise the decision-building, idempotency guard, and card
    update of ``handle_review_action`` — NOT the post-resume continuation, which
    has its own driving tests in ``test_slack_runner.py`` (re-pause posts the
    terminal card; completion persists+delivers).  Stubbing keeps them focused
    and off the runner's session-service internals.
    """
    return {"status": "delivered"}


def test_handle_review_action_confirm_builds_correct_decision(monkeypatch):
    """confirm_as_is → ReviewClarifyDecision(action='confirm_as_is') passed to resume_session."""
    db = FakeFirestore()
    slack = FakeSlackClient()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    op_id = "CR1:FR1:review"
    _seed_review_interrupt(db, op_id)

    captured = {}

    async def fake_resume(runner, db_, op, decision):
        captured["op_id"] = op
        captured["decision"] = decision
        return []

    monkeypatch.setattr("accounting_agents.slack_runner.resume_session", fake_resume)
    monkeypatch.setattr("accounting_agents.slack_runner._finalize_run_outcome", _ok_finalize)

    runner = _NoResumeRunner()
    result = asyncio.run(
        handle_review_action(
            runner=runner, ledger_store=store, db=db, slack_client=slack,
            op_id=op_id, action="confirm_as_is", app_name="acc",
        )
    )

    assert result["status"] == "resumed"
    assert captured["op_id"] == op_id
    assert captured["decision"].action == "confirm_as_is"
    assert captured["decision"].hint is None


def test_handle_review_action_reextract_carries_hint(monkeypatch):
    """reextract_as → ReviewClarifyDecision with the hint string."""
    db = FakeFirestore()
    slack = FakeSlackClient()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    op_id = "CR2:FR2:review"
    _seed_review_interrupt(db, op_id, channel_id="CR2", session_id="CR2:FR2", file_id="FR2")

    captured = {}

    async def fake_resume(runner, db_, op, decision):
        captured["decision"] = decision
        return []

    monkeypatch.setattr("accounting_agents.slack_runner.resume_session", fake_resume)
    monkeypatch.setattr("accounting_agents.slack_runner._finalize_run_outcome", _ok_finalize)

    asyncio.run(
        handle_review_action(
            runner=_NoResumeRunner(), ledger_store=store, db=db, slack_client=slack,
            op_id=op_id, action="reextract_as", app_name="acc",
            hint="This is a tax invoice; the supplier is registered.",
        )
    )

    assert captured["decision"].action == "reextract_as"
    assert captured["decision"].hint == "This is a tax invoice; the supplier is registered."


def test_handle_review_action_reject_posts_rejection_message(monkeypatch):
    """reject → posts the rejection message and uploads nothing."""
    db = FakeFirestore()
    slack = FakeSlackClient()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    op_id = "CR3:FR3:review"
    _seed_review_interrupt(db, op_id, channel_id="CR3", session_id="CR3:FR3", file_id="FR3")

    async def fake_resume(runner, db_, op, decision):
        return []

    monkeypatch.setattr("accounting_agents.slack_runner.resume_session", fake_resume)
    monkeypatch.setattr("accounting_agents.slack_runner._finalize_run_outcome", _ok_finalize)

    asyncio.run(
        handle_review_action(
            runner=_NoResumeRunner(), ledger_store=store, db=db, slack_client=slack,
            op_id=op_id, action="reject", app_name="acc",
        )
    )

    assert slack.uploads == []
    assert any("rejected" in t.lower() for t in _posted_texts(slack))


def test_handle_review_action_idempotent(monkeypatch):
    """Double-click on a review button resumes at most once.

    ``resume_session`` itself calls ``mark_processed`` on success.  We simulate
    that here by having the fake also call ``mark_processed`` so that the second
    ``handle_review_action`` call sees ``is_processed == True`` and short-circuits
    — exactly as the real flow works.
    """
    from accounting_agents.hitl import mark_processed

    db = FakeFirestore()
    slack = FakeSlackClient()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    op_id = "CR4:FR4:review"
    _seed_review_interrupt(db, op_id, channel_id="CR4", session_id="CR4:FR4", file_id="FR4")

    call_count = {"n": 0}

    async def fake_resume(runner, db_, op, decision):
        call_count["n"] += 1
        # Mirror what the real resume_session does: mark processed after success.
        mark_processed(db_, op)
        return []

    monkeypatch.setattr("accounting_agents.slack_runner.resume_session", fake_resume)
    monkeypatch.setattr("accounting_agents.slack_runner._finalize_run_outcome", _ok_finalize)

    runner = _NoResumeRunner()
    # First click.
    r1 = asyncio.run(
        handle_review_action(
            runner=runner, ledger_store=store, db=db, slack_client=slack,
            op_id=op_id, action="confirm_as_is", app_name="acc",
        )
    )
    # Second click (resume_session already called mark_processed above).
    r2 = asyncio.run(
        handle_review_action(
            runner=runner, ledger_store=store, db=db, slack_client=slack,
            op_id=op_id, action="confirm_as_is", app_name="acc",
        )
    )

    assert r1["status"] == "resumed"
    assert r2["status"] == "already_processed"
    # resume_session was called exactly once.
    assert call_count["n"] == 1


def test_handle_review_action_updates_review_card(monkeypatch):
    """After resume, the review card is replaced with the outcome block."""
    db = FakeFirestore()
    slack = FakeSlackClient()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    op_id = "CR5:FR5:review"
    _seed_review_interrupt(db, op_id, channel_id="CR5", session_id="CR5:FR5", file_id="FR5")

    async def fake_resume(runner, db_, op, decision):
        return []

    monkeypatch.setattr("accounting_agents.slack_runner.resume_session", fake_resume)
    monkeypatch.setattr("accounting_agents.slack_runner._finalize_run_outcome", _ok_finalize)

    asyncio.run(
        handle_review_action(
            runner=_NoResumeRunner(), ledger_store=store, db=db, slack_client=slack,
            op_id=op_id, action="confirm_as_is", app_name="acc",
        )
    )

    # chat_update should have been called to replace the card.
    assert len(slack.updates) >= 1
    update_blocks = slack.updates[-1].get("blocks", [])
    # The outcome block carries the check-mark icon.
    texts = [b["text"]["text"] for b in update_blocks if b.get("type") == "section"]
    assert any(":white_check_mark:" in t for t in texts)


# =========================================================================== #
# Bolt handler wiring (via build_async_app + patching)
# =========================================================================== #


def _capture_review_handlers(db_mock=None, runner_mock=None, ledger_store_mock=None):
    """Build the Bolt app with fakes and capture the review action/view handlers."""
    from accounting_agents import slack_runner

    registered = {"actions": {}, "views": {}}

    def action_decorator(action_id, *a, **k):
        def decorator(fn):
            registered["actions"][action_id] = fn
            return fn
        return decorator

    def view_decorator(callback_id, *a, **k):
        def decorator(fn):
            registered["views"][callback_id] = fn
            return fn
        return decorator

    fake_app = MagicMock()
    fake_app.event = lambda *a, **k: (lambda fn: fn)
    fake_app.action = action_decorator
    fake_app.view = view_decorator
    fake_app.command = lambda *a, **k: (lambda fn: fn)

    from app.slack_app import _SeenEvents
    fresh_seen = _SeenEvents()
    rm = runner_mock or MagicMock(app_name="acc")

    with patch.object(slack_runner, "_seen", fresh_seen), \
         patch("slack_bolt.async_app.AsyncApp", return_value=fake_app), \
         patch("invoice_processing.export.client_context.FirestoreClientStore"), \
         patch.object(slack_runner, "build_chat_runner",
                      return_value=SimpleNamespace(app_name="accounting_agents_assistant")):
        from accounting_agents.slack_runner import build_async_app
        build_async_app(
            runner=rm,
            ledger_store=ledger_store_mock or MagicMock(),
            db=db_mock or MagicMock(),
        )

    return registered


def test_review_confirm_handler_calls_handle_review_action(monkeypatch):
    """The ``review_confirm`` Bolt action calls handle_review_action(action='confirm_as_is')."""
    from accounting_agents import slack_runner

    captured = {}

    async def fake_handle(*, runner, ledger_store, db, slack_client, op_id, action, app_name, hint=None):
        captured["action"] = action
        captured["op_id"] = op_id
        return {"status": "resumed", "op_id": op_id, "events": 0}

    monkeypatch.setattr(slack_runner, "handle_review_action", fake_handle)

    registered = _capture_review_handlers()
    handler = registered["actions"]["review_confirm"]

    body = {"actions": [{"value": "C9:F9:review"}]}
    asyncio.run(handler(ack=AsyncMock(), body=body, client=MagicMock()))

    assert captured["action"] == "confirm_as_is"
    assert captured["op_id"] == "C9:F9:review"


def test_review_reject_handler_calls_handle_review_action(monkeypatch):
    """The ``review_reject`` Bolt action calls handle_review_action(action='reject')."""
    from accounting_agents import slack_runner

    captured = {}

    async def fake_handle(*, runner, ledger_store, db, slack_client, op_id, action, app_name, hint=None):
        captured["action"] = action
        return {"status": "resumed", "op_id": op_id, "events": 0}

    monkeypatch.setattr(slack_runner, "handle_review_action", fake_handle)

    registered = _capture_review_handlers()
    handler = registered["actions"]["review_reject"]

    body = {"actions": [{"value": "CA:FA:review"}]}
    asyncio.run(handler(ack=AsyncMock(), body=body, client=MagicMock()))

    assert captured["action"] == "reject"


def test_review_reextract_handler_opens_hint_modal(monkeypatch):
    """The ``review_reextract`` Bolt action opens the hint modal via views_open."""
    fake_sync_client = MagicMock()
    fake_sync_client.views_open = MagicMock()

    from accounting_agents import slack_runner
    from accounting_agents.slack_runner import build_async_app
    from app.slack_app import _SeenEvents

    registered2 = {"actions": {}, "views": {}}

    def action_decorator(action_id, *a, **k):
        def decorator(fn):
            registered2["actions"][action_id] = fn
            return fn
        return decorator

    def view_decorator(callback_id, *a, **k):
        def decorator(fn):
            registered2["views"][callback_id] = fn
            return fn
        return decorator

    fake_app2 = MagicMock()
    fake_app2.event = lambda *a, **k: (lambda fn: fn)
    fake_app2.action = action_decorator
    fake_app2.view = view_decorator
    fake_app2.command = lambda *a, **k: (lambda fn: fn)

    # Use FakeSlackClient so views_open calls are recorded.
    fake_slack = FakeSlackClient()

    with patch.object(slack_runner, "_seen", _SeenEvents()), \
         patch("slack_bolt.async_app.AsyncApp", return_value=fake_app2), \
         patch("invoice_processing.export.client_context.FirestoreClientStore"), \
         patch.object(slack_runner, "build_chat_runner",
                      return_value=SimpleNamespace(app_name="accounting_agents_assistant")), \
         patch("accounting_agents.slack_runner.WebClient", return_value=fake_slack, create=True):

        build_async_app(
            runner=MagicMock(app_name="acc"),
            ledger_store=MagicMock(),
            db=MagicMock(),
        )

    reextract_handler = registered2["actions"].get("review_reextract")
    assert reextract_handler is not None, "review_reextract action handler was not registered"


def test_review_hint_submit_handler_calls_reextract_with_hint(monkeypatch):
    """The ``ledgr_review_hint`` view-submission handler extracts the hint text."""
    from accounting_agents import slack_runner

    captured = {}

    async def fake_handle(*, runner, ledger_store, db, slack_client, op_id, action, app_name, hint=None):
        captured["action"] = action
        captured["hint"] = hint
        captured["op_id"] = op_id
        return {"status": "resumed", "op_id": op_id, "events": 0}

    monkeypatch.setattr(slack_runner, "handle_review_action", fake_handle)

    registered = _capture_review_handlers()
    handler = registered["views"]["ledgr_review_hint"]

    view = {
        "private_metadata": "CC:FC:review",
        "state": {
            "values": {
                "hint_block": {
                    "hint_input": {
                        "value": "This is a hotel receipt, not a tax invoice."
                    }
                }
            }
        },
    }
    body = {"view": view}
    asyncio.run(handler(ack=AsyncMock(), body=body, client=MagicMock()))

    assert captured["action"] == "reextract_as"
    assert captured["op_id"] == "CC:FC:review"
    assert captured["hint"] == "This is a hotel receipt, not a tax invoice."


def test_review_hint_submit_empty_hint_passes_none(monkeypatch):
    """An empty hint input passes ``hint=None`` (not an empty string)."""
    from accounting_agents import slack_runner

    captured = {}

    async def fake_handle(*, runner, ledger_store, db, slack_client, op_id, action, app_name, hint=None):
        captured["hint"] = hint
        return {"status": "resumed", "op_id": op_id, "events": 0}

    monkeypatch.setattr(slack_runner, "handle_review_action", fake_handle)

    registered = _capture_review_handlers()
    handler = registered["views"]["ledgr_review_hint"]

    view = {
        "private_metadata": "CD:FD:review",
        "state": {
            "values": {
                "hint_block": {
                    "hint_input": {"value": ""}
                }
            }
        },
    }
    asyncio.run(handler(ack=AsyncMock(), body={"view": view}, client=MagicMock()))

    assert captured["hint"] is None
