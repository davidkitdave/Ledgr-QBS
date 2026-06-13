"""Hermetic tests for the Slack runner (mock Slack + Firestore + ADK runner).

Covers:
- ``extract_final_text`` joins per-part text and NEVER reads ``content.text``
  (the reply-capture bug fix).
- ``find_interrupt_id`` detects an ``adk_request_input`` interrupt.
- ``process_file_event`` on the file path: downloads (mock) → save_artifact →
  run (fake runner) → on completion appends the ledger exactly once + posts.
- ``process_file_event`` on an interrupt: posts the Approve/Edit/Reject card and
  writes the Firestore interrupt correlation doc.
- ``handle_approval_action`` (approve) acks (handled by Bolt), resumes the real
  workflow, appends the ledger once, and is idempotent on a double-click.
"""

from __future__ import annotations

import asyncio

from types import SimpleNamespace

from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.workflow import START, Workflow, node
from google.genai import types

from accounting_agents import nodes
from accounting_agents.hitl import write_interrupt
from accounting_agents.ledger_store import SlackLedgerStore
from accounting_agents.nodes import ApproveDecision, approval_gate
from accounting_agents.sessions import FirestoreSessionService
from accounting_agents.slack_runner import (
    _derive_setup_prefill,
    build_async_app,
    deslugify_channel_name,
    event_node_name,
    event_stage_label,
    extract_final_text,
    find_interrupt_id,
    handle_approval_action,
    process_file_event,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice
from tests._fake_firestore import FakeFirestore
from tests.test_ledger_store import FakeSlackClient


# =========================================================================== #
# Pure helpers
# =========================================================================== #


def test_extract_final_text_joins_parts_not_content_text():
    content = SimpleNamespace(
        parts=[SimpleNamespace(text="Hello "), SimpleNamespace(text="world")],
        # A trap: a bare content.text the buggy code would have read instead.
        text="WRONG",
    )
    event = SimpleNamespace(content=content)
    assert extract_final_text(event) == "Hello world"


def test_extract_final_text_skips_non_text_parts():
    content = SimpleNamespace(
        parts=[SimpleNamespace(text=None), SimpleNamespace(text="only this")]
    )
    event = SimpleNamespace(content=content)
    assert extract_final_text(event) == "only this"


def test_find_interrupt_id():
    fc = SimpleNamespace(name="adk_request_input", id="C1:F1")
    event = SimpleNamespace(get_function_calls=lambda: [fc])
    assert find_interrupt_id(event) == "C1:F1"
    none_event = SimpleNamespace(get_function_calls=lambda: [])
    assert find_interrupt_id(none_event) is None


# =========================================================================== #
# Fake ADK runner for the file path
# =========================================================================== #


class _FakeArtifactService:
    def __init__(self):
        self.saved = {}

    async def save_artifact(self, *, app_name, user_id, filename, artifact, session_id=None, custom_metadata=None):
        self.saved[(user_id, filename)] = artifact
        return 0


class _FakeSession:
    def __init__(self, state):
        self.state = dict(state)


class _FakeSessionService:
    def __init__(self, final_state):
        self._final_state = final_state
        self.created = False

    async def get_session(self, *, app_name, user_id, session_id):
        # Before run: no session. After run: returns the final state.
        if not self.created:
            return None
        return _FakeSession(self._final_state)

    async def create_session(self, *, app_name, user_id, session_id, state=None):
        self.created = True
        return _FakeSession(state or {})


class _FakeRunner:
    """Minimal runner stand-in for the file path; yields scripted events."""

    def __init__(self, events, final_state, app_name="acc"):
        self.app_name = app_name
        self.artifact_service = _FakeArtifactService()
        self.session_service = _FakeSessionService(final_state)
        self._events = events

    async def run_async(self, *, user_id, session_id, new_message=None, state_delta=None):
        self.session_service.created = True
        for ev in self._events:
            yield ev


def _ledger_payload(sheet="Purchase", doc_key="F1:Purchase:INV-1"):
    return {
        nodes.LEDGER_ROWS_KEY: {
            "client_id": "c1",
            "fy": "2026",
            "kind": "invoice",
            "software": "qbs",
            "batches": [
                {
                    "sheet": sheet,
                    "doc_key": doc_key,
                    "rows": [{"Invoice Number": "INV-1", "Description": "x", "Source Amount": 10.0}],
                }
            ],
        },
        nodes.DELIVER_SUMMARY_KEY: "📒 Added 1 line from 1 document to your FY2026 ledger.",
    }


def test_process_file_event_completion_appends_ledger_once():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    final_event = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text="done")]),
        get_function_calls=lambda: [],
    )
    runner = _FakeRunner([final_event], _ledger_payload())

    downloaded = {}

    def fake_download(client, file_id):
        downloaded["file_id"] = file_id
        return b"%PDF-1.4 fake"

    result = asyncio.run(
        process_file_event(
            runner=runner,
            ledger_store=store,
            db=db,
            slack_client=slack,
            channel_id="C1",
            file_id="F1",
            app_name="acc",
            download_fn=fake_download,
            source_filename="invoice.pdf",
        )
    )

    assert result["status"] == "delivered"
    # The PDF was saved as an artifact under the convention name.
    assert ("C1", "inbox/F1.pdf") in runner.artifact_service.saved
    assert downloaded["file_id"] == "F1"
    # The ledger workbook was uploaded exactly once, with one row.
    assert len(slack.uploads) == 1
    assert result["append"]["appended"] == 1
    # The delivery summary was posted.
    assert any("FY2026 ledger" in u for u in _posted_texts(slack))


def test_process_file_event_interrupt_posts_card_and_writes_doc():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    interrupt_event = SimpleNamespace(
        content=SimpleNamespace(parts=[]),
        get_function_calls=lambda: [SimpleNamespace(name="adk_request_input", id="C1:F1")],
    )
    runner = _FakeRunner([interrupt_event], {"approval_message": "needs review: line X"})

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
        )
    )

    assert result["status"] == "paused"
    assert result["op_id"] == "C1:F1"
    # No ledger uploaded while paused.
    assert slack.uploads == []
    # The approval card was posted (carries the three action buttons).
    card = _last_blocks(slack)
    action_ids = {
        e["action_id"]
        for b in card
        if b.get("type") == "actions"
        for e in b["elements"]
    }
    assert action_ids == {"approve", "edit", "reject"}
    # The interrupt correlation doc was written to Firestore.
    snap = db.collection("interrupts").document("C1:F1").get()
    assert snap.exists
    assert snap.to_dict()["slack_file_id"] == "F1"


# =========================================================================== #
# Approve action → resume → append once (idempotent), using a REAL runner
# =========================================================================== #

_DELIVER_RUNS: list = []


@node
async def _terminal(ctx, node_input) -> Event:
    """Stand-in successor of the gate: stage a ledger payload for persistence."""
    _DELIVER_RUNS.append(node_input)
    ctx.state[nodes.LEDGER_ROWS_KEY] = {
        "client_id": "c1",
        "fy": "2026",
        "kind": "invoice",
        "software": "qbs",
        "batches": [
            {
                "sheet": "Purchase",
                "doc_key": "F7:Purchase:INV-7",
                "rows": [{"Invoice Number": "INV-7", "Description": "y", "Source Amount": 20.0}],
            }
        ],
    }
    ctx.state[nodes.DELIVER_SUMMARY_KEY] = "📒 Added 1 line from 1 document to your FY2026 ledger."
    return Event(output={"ok": True})


def _build_resumable_app() -> App:
    wf = Workflow(name="approve_wf", edges=[(START, approval_gate, _terminal)])
    return App(
        name="acc",
        root_agent=wf,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )


def _low_conf_state() -> dict:
    inv = NormalizedInvoice(
        invoice_number="INV-7",
        reconciled=True,
        lines=[InvoiceLine(description="ambiguous", tax_confidence=0.30)],
    )
    return {
        "op_id": "C7:F7",
        "channel_id": "C7",
        "file_id": "F7",
        nodes.NORMALIZED_KEY: [nodes._inv_to_dict(inv)],
    }


def setup_function(_):
    _DELIVER_RUNS.clear()


def test_approve_action_resumes_and_appends_once_idempotent():
    slack = FakeSlackClient()
    db = FakeFirestore()
    ledger_store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    svc = FirestoreSessionService(client=db)
    asyncio.run(
        svc.create_session(app_name="acc", user_id="C7", session_id="C7", state=_low_conf_state())
    )
    runner = Runner(app=_build_resumable_app(), session_service=svc)

    # Drive to the HITL pause.
    async def drive():
        async for _ev in runner.run_async(
            user_id="C7",
            session_id="C7",
            new_message=types.Content(parts=[types.Part(text="process")]),
        ):
            pass

    asyncio.run(drive())
    assert _DELIVER_RUNS == []

    # The Slack layer posted the card + wrote the correlation doc.
    write_interrupt(
        db, "C7:F7", session_id="C7", channel_id="C7", slack_file_id="F7", message_ts="111.222",
        extra={"summary": "needs review"},
    )

    # First click: resume, append the ledger once, update the card.
    res1 = asyncio.run(
        handle_approval_action(
            runner=runner,
            ledger_store=ledger_store,
            db=db,
            slack_client=slack,
            op_id="C7:F7",
            decision="approve",
            app_name="acc",
        )
    )
    assert res1["status"] == "resumed"
    assert len(_DELIVER_RUNS) == 1
    assert len(slack.uploads) == 1
    assert res1["append"]["appended"] == 1

    # Second click (double-click): no-op — no second resume, no second upload.
    res2 = asyncio.run(
        handle_approval_action(
            runner=runner,
            ledger_store=ledger_store,
            db=db,
            slack_client=slack,
            op_id="C7:F7",
            decision="approve",
            app_name="acc",
        )
    )
    assert res2["status"] == "already_processed"
    assert len(_DELIVER_RUNS) == 1
    assert len(slack.uploads) == 1


def test_reject_action_resumes_without_appending():
    slack = FakeSlackClient()
    db = FakeFirestore()
    ledger_store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    svc = FirestoreSessionService(client=db)
    asyncio.run(
        svc.create_session(app_name="acc", user_id="C8", session_id="C8", state={
            "op_id": "C8:F8",
            "channel_id": "C8",
            "file_id": "F8",
            nodes.NORMALIZED_KEY: _low_conf_state()[nodes.NORMALIZED_KEY],
        })
    )
    runner = Runner(app=_build_resumable_app(), session_service=svc)

    async def drive():
        async for _ev in runner.run_async(
            user_id="C8", session_id="C8",
            new_message=types.Content(parts=[types.Part(text="process")]),
        ):
            pass

    asyncio.run(drive())
    write_interrupt(db, "C8:F8", session_id="C8", channel_id="C8", slack_file_id="F8", message_ts="9.9")

    res = asyncio.run(
        handle_approval_action(
            runner=runner, ledger_store=ledger_store, db=db, slack_client=slack,
            op_id="C8:F8", decision="reject", app_name="acc",
        )
    )
    assert res["status"] == "resumed"
    # Reject path uploads nothing to the ledger.
    assert slack.uploads == []


# =========================================================================== #
# Stage detection + live status message
# =========================================================================== #


def _node_event(node_name: str, *, text: str = "", interrupt: bool = False):
    """A scripted event tagged with a node path the way the ADK Workflow does."""
    calls = (
        [SimpleNamespace(name="adk_request_input", id="C1:F1")] if interrupt else []
    )
    return SimpleNamespace(
        node_info=SimpleNamespace(path=f"document_workflow@1/{node_name}@1"),
        content=SimpleNamespace(parts=[SimpleNamespace(text=text)] if text else []),
        get_function_calls=lambda: calls,
    )


def test_event_node_name_parses_trailing_node():
    ev = _node_event("classify_node")
    assert event_node_name(ev) == "classify_node"
    # No node_info / empty path → None (e.g. coordinator chatter).
    assert event_node_name(SimpleNamespace()) is None
    assert event_node_name(SimpleNamespace(node_info=SimpleNamespace(path=""))) is None


def test_event_stage_label_maps_known_nodes():
    assert "Classifying" in event_stage_label(_node_event("classify_node"))
    assert "bank statement" in event_stage_label(_node_event("extract_bank_node"))
    assert "invoice" in event_stage_label(_node_event("extract_invoice_node"))
    # Unmapped / untagged events do not trigger a status change.
    assert event_stage_label(_node_event("some_unknown_node")) is None
    assert event_stage_label(SimpleNamespace()) is None


def test_process_file_event_posts_status_once_and_updates_per_stage():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    # A realistic single-document stream: classify → extract → tax → final text.
    events = [
        _node_event("classify_node"),
        _node_event("extract_invoice_node"),
        _node_event("tax_node"),
        _node_event("deliver_node", text="done"),
    ]
    runner = _FakeRunner(events, _ledger_payload())

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
            source_filename="invoice.pdf",
        )
    )
    assert result["status"] == "delivered"

    # Exactly ONE status message was posted (the initial "received" message). The
    # delivery summary is a separate post; assert the status post is distinct and
    # there is only one "received" message.
    received = [p for p in _post_calls(slack) if "Received" in p.get("text", "")]
    assert len(received) == 1
    assert "invoice.pdf" in received[0]["text"]

    # The status message was edited in place through the stages (distinct labels)
    # plus the terminal ✅. The status post is the FIRST chat_postMessage, whose
    # returned ts is "1.000" (FakeSlackClient numbers posts 1-based).
    status_ts = "1.000"
    update_texts = [u["text"] for u in slack.updates]
    assert any("Classifying" in t for t in update_texts)
    assert any("Extracting" in t for t in update_texts)
    assert any("Reconciling" in t for t in update_texts)
    assert update_texts[-1] == "✅ Processed"
    # Every update targeted the single status message ts.
    assert all(u["ts"] == status_ts for u in slack.updates)


def test_instant_ack_reaction_and_status_before_semaphore_heavy_work():
    """👀 reaction + initial status message fire BEFORE the download (semaphore-guarded).

    Strategy: the download fn blocks on a threading.Event until we confirm both
    the reaction and the status post have already happened, then unblocks.
    This proves the ack is outside/before the semaphore-guarded heavy work.
    """
    import threading

    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    # Teach the fake client that file F1 was shared in channel C1 at ts "9.001".
    slack._file_share_ts["F1"] = {"C1": "9.001"}

    # The download blocks until we release it.
    download_started = threading.Event()
    download_may_proceed = threading.Event()

    def blocking_download(client, file_id):
        download_started.set()
        download_may_proceed.wait(timeout=5)
        return b"%PDF-1.4 fake"

    # Run process_file_event in a background thread so we can inspect state
    # while the download is blocked.
    result_holder: list = []

    async def _run():
        r = await process_file_event(
            runner=_FakeRunner([
                SimpleNamespace(
                    content=SimpleNamespace(parts=[SimpleNamespace(text="done")]),
                    get_function_calls=lambda: [],
                )
            ], _ledger_payload()),
            ledger_store=store,
            db=db,
            slack_client=slack,
            channel_id="C1",
            file_id="F1",
            app_name="acc",
            download_fn=blocking_download,
            source_filename="invoice.pdf",
        )
        result_holder.append(r)

    t = threading.Thread(target=lambda: asyncio.run(_run()), daemon=True)
    t.start()

    # Wait for the download to start (we are now inside the semaphore).
    assert download_started.wait(timeout=5), "download never started"

    # At this point the download is blocked inside _SEM. Assert that the INSTANT
    # ack (reaction + status post) already happened BEFORE we entered _SEM.
    assert any(r["name"] == "eyes" and r["timestamp"] == "9.001"
               for r in slack.reactions_added), \
        "👀 reaction must be added before semaphore-guarded download starts"

    received_posts = [p for p in slack._posts if "on it" in p.get("text", "")]
    assert received_posts, "initial status message must be posted before download starts"

    # Release the download and wait for the run to finish.
    download_may_proceed.set()
    t.join(timeout=10)

    assert result_holder and result_holder[0]["status"] == "delivered"
    # On completion the 👀 is removed and ✅ added.
    assert any(r["name"] == "eyes" for r in slack.reactions_removed), \
        "👀 reaction must be removed on completion"
    assert any(r["name"] == "white_check_mark" for r in slack.reactions_added), \
        "✅ reaction must be added on completion"


def test_process_file_event_status_update_failure_does_not_crash_run():
    class _FlakyUpdateClient(FakeSlackClient):
        def chat_update(self, **kwargs):
            raise RuntimeError("slack 500")

    slack = _FlakyUpdateClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    runner = _FakeRunner([_node_event("classify_node", text="done")], _ledger_payload())

    # The cosmetic chat_update failures must NOT abort processing.
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
        )
    )
    assert result["status"] == "delivered"
    assert len(slack.uploads) == 1


def test_process_file_event_interrupt_sets_needs_review_status():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    events = [
        _node_event("classify_node"),
        _node_event("approval_gate", interrupt=True),
    ]
    runner = _FakeRunner(events, {"approval_message": "needs review: line X"})

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
        )
    )
    assert result["status"] == "paused"
    update_texts = [u["text"] for u in slack.updates]
    assert any("Needs your review" in t for t in update_texts)


# =========================================================================== #
# De-slugify channel name
# =========================================================================== #


def test_deslugify_channel_name_basic():
    assert deslugify_channel_name("akar-enterprises-pte-ltd") == "Akar Enterprises Pte Ltd"


def test_deslugify_channel_name_underscores_and_suffixes():
    assert deslugify_channel_name("foo_bar_llp") == "Foo Bar LLP"
    assert deslugify_channel_name("acme-sg-pte-ltd") == "Acme SG Pte Ltd"


def test_deslugify_channel_name_empty():
    assert deslugify_channel_name("") == ""
    assert deslugify_channel_name("---") == ""


# =========================================================================== #
# Setup-open prefill from channel name
# =========================================================================== #


def test_derive_setup_prefill_from_channel_name():
    class _InfoClient:
        def conversations_info(self, *, channel):
            assert channel == "C-OPEN"
            return {"ok": True, "channel": {"id": channel, "name": "akar-enterprises-pte-ltd"}}

    body = {"channel": {"id": "C-OPEN"}, "trigger_id": "t1"}
    prefill = asyncio.run(_derive_setup_prefill(_InfoClient(), body))
    assert prefill == {"client_name": "Akar Enterprises Pte Ltd"}


def test_derive_setup_prefill_handles_lookup_failure():
    class _BoomClient:
        def conversations_info(self, *, channel):
            raise RuntimeError("missing_scope")

    body = {"channel": {"id": "C-OPEN"}}
    assert asyncio.run(_derive_setup_prefill(_BoomClient(), body)) is None


def test_derive_setup_prefill_no_channel_returns_none():
    assert asyncio.run(_derive_setup_prefill(object(), {})) is None


# =========================================================================== #
# helpers
# =========================================================================== #


def _post_calls(slack: FakeSlackClient) -> list:
    return getattr(slack, "_posts", [])


def _posted_texts(slack: FakeSlackClient) -> list:
    return [p.get("text", "") for p in _post_calls(slack)]


def _last_blocks(slack: FakeSlackClient):
    for p in reversed(_post_calls(slack)):
        if p.get("blocks"):
            return p["blocks"]
    return []


# =========================================================================== #
# message/file_share wiring: uploads delivered via the message event path
# =========================================================================== #


def _capture_message_handler(runner_mock=None, ledger_store_mock=None, db_mock=None):
    """Build the Bolt app with fakes and return the registered ``message`` handler."""
    from unittest.mock import MagicMock, patch

    from app.slack_app import _SeenEvents
    from accounting_agents import slack_runner

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

    fresh_seen = _SeenEvents()
    rm = runner_mock or MagicMock()
    rm.app_name = "acc"

    with patch.object(slack_runner, "_seen", fresh_seen), \
         patch("slack_bolt.async_app.AsyncApp", return_value=fake_app), \
         patch("invoice_processing.export.client_context.InMemoryClientStore"):
        build_async_app(
            runner=rm,
            ledger_store=ledger_store_mock or MagicMock(),
            db=db_mock or MagicMock(),
        )

    return registered["message"], fresh_seen


def test_message_file_share_calls_process_file_event_per_file():
    """A message/file_share event with 2 files calls process_file_event twice."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from accounting_agents import slack_runner

    handler, _ = _capture_message_handler()

    body = {"event_id": "Ev-file-1"}
    event = {
        "type": "message",
        "subtype": "file_share",
        "ts": "111.001",
        "channel": "C-file",
        "files": [{"id": "FA1"}, {"id": "FA2"}],
    }
    fake_client = MagicMock()
    mock_pfe = AsyncMock(return_value={"status": "delivered"})

    with patch.object(slack_runner, "process_file_event", mock_pfe), \
         patch.object(slack_runner, "download_pdf_bytes", return_value=b"%PDF fake"):
        asyncio.run(handler(event=event, body=body, client=fake_client))

    assert mock_pfe.call_count == 2
    called_file_ids = {c.kwargs["file_id"] for c in mock_pfe.call_args_list}
    assert called_file_ids == {"FA1", "FA2"}
    # channel_id threaded through correctly
    assert all(c.kwargs["channel_id"] == "C-file" for c in mock_pfe.call_args_list)


def test_message_file_share_dedup_not_reprocessed():
    """Same event_id redelivered → process_file_event called only once (not twice)."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from accounting_agents import slack_runner

    handler, _ = _capture_message_handler()

    body = {"event_id": "Ev-file-dup"}
    event = {
        "type": "message",
        "subtype": "file_share",
        "ts": "111.002",
        "channel": "C-file",
        "files": [{"id": "FB1"}],
    }
    fake_client = MagicMock()
    mock_pfe = AsyncMock(return_value={"status": "delivered"})

    with patch.object(slack_runner, "process_file_event", mock_pfe), \
         patch.object(slack_runner, "download_pdf_bytes", return_value=b"%PDF fake"):
        # First delivery
        asyncio.run(handler(event=event, body=body, client=fake_client))
        assert mock_pfe.call_count == 1
        # Duplicate delivery — same event_id
        asyncio.run(handler(event=event, body=body, client=fake_client))
        assert mock_pfe.call_count == 1  # unchanged


def test_message_text_only_routes_to_question_not_file():
    """A plain text message (no files, no subtype) goes to answer_question only."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from accounting_agents import slack_runner

    handler, _ = _capture_message_handler()

    body = {"event_id": "Ev-text-1"}
    event = {
        "type": "message",
        "ts": "111.003",
        "channel": "C-qa",
        "text": "What is my GST balance?",
    }
    fake_client = MagicMock()
    mock_pfe = AsyncMock(return_value={"status": "delivered"})
    mock_aq = AsyncMock(return_value={"status": "answered", "text": "42"})

    with patch.object(slack_runner, "process_file_event", mock_pfe), \
         patch.object(slack_runner, "answer_question", mock_aq):
        asyncio.run(handler(event=event, body=body, client=fake_client))

    mock_pfe.assert_not_called()
    mock_aq.assert_called_once()
    assert mock_aq.call_args.kwargs["question"] == "What is my GST balance?"
    assert mock_aq.call_args.kwargs["channel_id"] == "C-qa"
