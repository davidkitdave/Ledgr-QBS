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
        nodes.DELIVER_SUMMARY_KEY: "Added 1 line to your FY2026 ledger (auto_approved).",
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
    ctx.state[nodes.DELIVER_SUMMARY_KEY] = "Added 1 line to your FY2026 ledger (approve)."
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
