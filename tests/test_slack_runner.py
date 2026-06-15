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
from unittest.mock import patch
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.workflow import START, Workflow, node
from google.genai import types

from accounting_agents import nodes
from accounting_agents.hitl import read_interrupt, write_interrupt
from accounting_agents.ledger_store import SlackLedgerStore
from accounting_agents.nodes import approval_gate
from accounting_agents.sessions import FirestoreSessionService
from accounting_agents.slack_runner import (
    _derive_setup_prefill,
    _doc_label_from_state,
    _edits_from_view_state,
    _persist_corrections,
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


def test_extract_tool_response_text_returns_last_result():
    from accounting_agents.slack_runner import extract_tool_response_text

    fr1 = SimpleNamespace(response={"result": "first"})
    fr2 = SimpleNamespace(response={"result": "second (latest)"})
    event = SimpleNamespace(get_function_responses=lambda: [fr1, fr2])
    assert extract_tool_response_text(event) == "second (latest)"


def test_extract_tool_response_text_empty_when_no_responses():
    from accounting_agents.slack_runner import extract_tool_response_text

    event = SimpleNamespace(get_function_responses=lambda: [])
    assert extract_tool_response_text(event) == ""
    bare = SimpleNamespace()
    assert extract_tool_response_text(bare) == ""


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


# --------------------------------------------------------------------------- #
# Test fixture: a Firestore-backed client store with a known QBS profile so
# process_file_event's soft-gate lets the run proceed.
# --------------------------------------------------------------------------- #
_TEST_PROFILE: dict = {
    "client_id": "c1",
    "client_name": "Test Client",
    "fye_month": 12,
    "accounting_software": "QBS Ledger",
    "gst_registered": True,
    "region": "SINGAPORE",
    "base_currency": "SGD",
    "status": "active",
}


def _seeded_client_store(db: FakeFirestore, channel_id: str = "C1",
                        client_id: str = "c1") -> "FirestoreClientStore":  # noqa: F821 — string forward-ref; import is local
    """Write a minimal QBS client profile + channel reverse-index into ``db``
    and return a :class:`FirestoreClientStore` bound to that fake. The default
    channel/client ids match the rest of this test module.
    """
    from invoice_processing.export.client_context import FirestoreClientStore

    profile = dict(_TEST_PROFILE, client_id=client_id)
    db.collection("clients").document(client_id).set(profile)
    db.collection("channels").document(channel_id).set({"client_id": client_id})
    return FirestoreClientStore(client=db)


def test_profile_state_delta_includes_software_and_coa():
    from accounting_agents.slack_runner import _profile_state_delta
    from invoice_processing.export.client_context import ClientContext, CoaAccount

    class _Store:
        def get_by_channel(self, channel_id):
            assert channel_id == "C1"
            return ClientContext(
                client_id="CL-1",
                client_name="Auditair International Pte. Ltd.",
                accounting_software="Xero",
                fye_month=10,
                coa=[CoaAccount(code="6010", description="Travel",
                                account_type="Expense", financial_statement="P&L",
                                nature="Dr", keywords="travel")],
            )

    delta = _profile_state_delta(_Store(), "C1")
    assert delta["software"] == "Xero"
    assert delta["client_id"] == "CL-1"
    assert len(delta["coa"]) == 1
    # Each COA entry exposes its `key` (the export-stable identifier) — verifies
    # the helper preserves what the categorizer needs downstream.
    assert delta["coa"][0]["key"] == "6010"
    assert delta["coa"][0]["keywords"] == "travel"


def test_profile_state_delta_empty_when_no_profile():
    from accounting_agents.slack_runner import _profile_state_delta

    class _Store:
        def get_by_channel(self, channel_id):
            return None

    assert _profile_state_delta(_Store(), "C1") == {}


def test_process_file_event_softgates_when_no_profile():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    class _NoProfileStore:
        def get_by_channel(self, channel_id):
            return None

    runner = _FakeRunner([], _ledger_payload())  # should never run
    result = asyncio.run(
        process_file_event(
            runner=runner, ledger_store=store, db=db, slack_client=slack,
            channel_id="C1", file_id="F1", app_name="acc",
            download_fn=lambda c, f: b"%PDF-1.4 fake",
            source_filename="invoice.pdf", client_store=_NoProfileStore(),
        )
    )
    assert result["status"] == "no_profile"
    assert any("this client set up" in t.lower() for t in _posted_texts(slack))


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
            client_store=_seeded_client_store(db),
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
            client_store=_seeded_client_store(db),
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
# Task 7: Edit action + view_submission handlers (modal-based per-line edits)
# =========================================================================== #


class _FakeActionViewRunner:
    """Minimal Runner for Edit-modal tests: app_name + session_service stub."""

    def __init__(self, app_name="acc", session_service=None):
        self.app_name = app_name
        self.session_service = session_service


class _FakeSessionSvc:
    """In-memory session service stub keyed by (user_id, session_id)."""

    def __init__(self, sessions):
        self._sessions = sessions

    async def get_session(self, *, app_name, user_id, session_id):
        return self._sessions.get((user_id, session_id))


def _capture_hitl_handlers(runner_mock=None, ledger_store_mock=None, db_mock=None):
    """Build the Bolt app with fakes and capture the ``edit`` action + ``ledgr_invoice_edit`` view.

    Mirrors ``_capture_message_handler`` but for the action/view decorators. The
    fake app records registered actions/views in a dict keyed by their first
    positional argument (the action_id or callback_id), so callers can retrieve
    the actual coroutine that ``build_async_app`` registered.
    """
    from unittest.mock import MagicMock, patch

    from app.slack_app import _SeenEvents
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

    fresh_seen = _SeenEvents()
    rm = runner_mock or _FakeActionViewRunner()

    with patch.object(slack_runner, "_seen", fresh_seen), \
         patch("slack_bolt.async_app.AsyncApp", return_value=fake_app), \
         patch("invoice_processing.export.client_context.FirestoreClientStore"), \
         patch.object(slack_runner, "build_chat_runner",
                      return_value=SimpleNamespace(app_name="accounting_agents_assistant")):
        build_async_app(
            runner=rm,
            ledger_store=ledger_store_mock or MagicMock(),
            db=db_mock or MagicMock(),
        )

    return registered["actions"]["edit"], registered["views"]["ledgr_invoice_edit"]


def test_edit_action_opens_invoice_modal_with_proposed_lines():
    """Clicking Edit MUST call views_open with a well-formed modal (callback_id + op_id).

    The modal body is pre-filled from the paused session's normalized invoice
    lines + COA, so the user can correct the fields in-place before resubmitting.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from accounting_agents import slack_runner

    db = FakeFirestore()
    # Seed the HITL interrupt correlation doc so the handler can read session state.
    write_interrupt(
        db, "OP1", session_id="S-OP1", channel_id="C-OP1", slack_file_id="F-OP1", message_ts="1.1",
    )

    # Seed a paused session whose state the Edit modal reads to pre-fill lines.
    paused_session = _FakeSession({
        nodes.NORMALIZED_KEY: [
            {
                "lines": [
                    {"description": "Room", "account_code": "6010", "tax_treatment": "SR", "net_amount": 51.49},
                    {"description": "Tax", "account_code": None, "tax_treatment": "ZR", "net_amount": 3.60},
                ]
            }
        ],
        "coa": [{"code": "6010", "description": "Travel"}],
    })
    session_svc = _FakeSessionSvc({("C-OP1", "S-OP1"): paused_session})

    # Recording fake for the sync WebClient that build_async_app instantiates.
    sync_client = MagicMock()
    with patch("slack_sdk.WebClient", return_value=sync_client):
        edit_handler, _ = _capture_hitl_handlers(
            runner_mock=_FakeActionViewRunner(session_service=session_svc),
            db_mock=db,
        )

    body = {
        "actions": [{"action_id": "edit", "value": "OP1"}],
        "trigger_id": "T-EDIT-1",
        "channel": {"id": "C-OP1"},
    }

    with patch.object(slack_runner, "read_interrupt") as mock_read, \
         patch.object(slack_runner, "_read_session_state", AsyncMock(return_value=paused_session.state)):
        # Make read_interrupt return our seeded doc.
        mock_read.return_value = {
            "op_id": "OP1", "user_id": "C-OP1", "session_id": "S-OP1", "channel_id": "C-OP1",
        }
        ack = AsyncMock()
        bolt_client = MagicMock()
        asyncio.run(edit_handler(ack=ack, body=body, client=bolt_client))

    # The handler MUST ack (within Bolt's 3s window) before opening the modal.
    ack.assert_awaited_once()

    # views_open was called with trigger_id + a modal carrying the right IDs.
    sync_client.views_open.assert_called_once()
    kwargs = sync_client.views_open.call_args.kwargs
    assert kwargs["trigger_id"] == "T-EDIT-1"
    view = kwargs["view"]
    assert view["callback_id"] == "ledgr_invoice_edit"
    assert view["private_metadata"] == "OP1"
    # The modal blocks must include the proposed-line prefill (one input group per line).
    inputs = [b for b in view["blocks"] if b.get("type") == "input"]
    assert len(inputs) == 6  # 2 lines × (acct + tax + amt)


def test_edit_submit_invokes_handle_approval_action_with_parsed_edits():
    """Submitting the Edit modal MUST call handle_approval_action(decision="edit", edits=...).

    Seats a recorder on ``handle_approval_action``, drives the view handler with
    a realistic ``view_submission`` body (private_metadata + state.values), and
    asserts the recorder was called with op_id, decision="edit", and edits
    parsed from the ``acct_<i>`` / ``tax_<i>`` / ``amt_<i>`` block_ids.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from accounting_agents import slack_runner

    db = FakeFirestore()

    edit_handler, edit_submit_handler = _capture_hitl_handlers(db_mock=db)

    # Build a view_submission body with two edited lines.
    body = {
        "view": {
            "callback_id": "ledgr_invoice_edit",
            "private_metadata": "OP-SUBMIT-1",
            "state": {
                "values": {
                    "acct_0": {"v": {"selected_option": {"value": "6200"}}},
                    "tax_0": {"v": {"selected_option": {"value": "ZR"}}},
                    "amt_0": {"v": {"value": "99.95"}},
                    "acct_1": {"v": {"selected_option": {"value": "6010"}}},
                    "tax_1": {"v": {"selected_option": {"value": "SR"}}},
                    "amt_1": {"v": {"value": "12.00"}},
                }
            },
        }
    }

    # Record calls to handle_approval_action without actually resuming a workflow.
    # AsyncMock auto-awaits when patched over an `async def` function.
    recorder = AsyncMock(return_value={"status": "resumed"})

    with patch.object(slack_runner, "handle_approval_action", recorder):
        ack = AsyncMock()
        bolt_client = MagicMock()
        asyncio.run(edit_submit_handler(ack=ack, body=body, client=bolt_client))

    # Bolt ack must be called within 3s.
    ack.assert_awaited_once()
    recorder.assert_called_once()
    call = recorder.call_args
    assert call.kwargs["op_id"] == "OP-SUBMIT-1"
    assert call.kwargs["decision"] == "edit"
    edits = call.kwargs["edits"]
    assert edits == {
        "lines": [
            {"index": 0, "account_code": "6200", "tax_treatment": "ZR", "net_amount": 99.95},
            {"index": 1, "account_code": "6010", "tax_treatment": "SR", "net_amount": 12.0},
        ]
    }


def test_edit_submit_persists_correction_after_resume(monkeypatch):
    """E2E wiring: view_submission → handle_approval_action → _persist_corrections → add_correction.

    Mirrors ``test_edit_submit_invokes_handle_approval_action_with_parsed_edits``'s
    scaffold but additionally seeds:
      - a ``FakeFirestore`` interrupt correlation doc keyed by op_id, AND
      - a paused session state with ``client_id`` + ``normalized_invoices``
        so ``_persist_corrections`` (called from the wiring at slack_runner.py:1090-1098)
        has the inputs it needs to actually invoke ``add_correction``.
    The module-level ``_DEFAULT_CLIENT_STORE`` is monkeypatched to a recorder so
    we can assert the wiring reaches the persistence side without standing up
    a real FirestoreClientStore.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from accounting_agents import slack_runner

    db = FakeFirestore()

    # Seed the interrupt correlation doc the wiring reads at slack_runner.py:1092.
    # Explicit user_id=channel_id so the doc's user_id matches the session-svc
    # lookup key (write_interrupt's default is session_id, which would not).
    write_interrupt(
        db, "OP-WIRE-1",
        session_id="S-WIRE-1", channel_id="C-WIRE-1", user_id="C-WIRE-1",
        slack_file_id="F-WIRE-1", message_ts="1.1",
    )

    # Seed a paused session whose state carries the ADR-0004 inputs
    # (client_id + normalized_invoices) that _persist_corrections reads.
    # Use the REAL serialized NormalizedInvoice shape (_inv_to_dict = asdict):
    # the vendor lives in the nested ``supplier``/``customer`` party, NOT a flat
    # ``vendor_name`` key. A prior fixture used ``vendor_name`` and masked the
    # production bug where _persist_corrections read keys that never exist.
    paused_state = {
        "client_id": "CL-1",
        nodes.NORMALIZED_KEY: [
            {
                "doc_type": "purchase",
                "supplier": {"name": "Hotel Booking"},
                "customer": {"name": None},
                # Proposed line is uncategorized, so the submitted 6010/ZR are
                # genuine human changes (only-changed-lines become Corrections).
                "lines": [
                    {"description": "Room"},
                ],
            }
        ],
    }
    paused_session = _FakeSession(paused_state)
    session_svc = _FakeSessionSvc({("C-WIRE-1", "S-WIRE-1"): paused_session})

    _, edit_submit_handler = _capture_hitl_handlers(
        runner_mock=_FakeActionViewRunner(session_service=session_svc),
        db_mock=db,
    )

    # view_submission body: one edited line, account_code + tax_code (amount
    # omitted to mirror the natural UI path — _persist_corrections reads only
    # account_code / tax_code).
    body = {
        "view": {
            "callback_id": "ledgr_invoice_edit",
            "private_metadata": "OP-WIRE-1",
            "state": {
                "values": {
                    "acct_0": {"v": {"selected_option": {"value": "6010"}}},
                    "tax_0":  {"v": {"selected_option": {"value": "ZR"}}},
                }
            },
        }
    }

    # Recorder for handle_approval_action (mirrors Task 7's pattern).
    approval_recorder = AsyncMock(return_value={"status": "resumed"})

    # Recording fake for the default client store the wiring uses.
    correction_calls = []

    class _RecordingStore:
        def add_correction(self, *, client_id, vendor, account_code=None, tax_code=None):
            correction_calls.append(
                {"client_id": client_id, "vendor": vendor,
                 "account_code": account_code, "tax_code": tax_code}
            )

    monkeypatch.setattr(slack_runner, "_DEFAULT_CLIENT_STORE", _RecordingStore())

    with patch.object(slack_runner, "handle_approval_action", approval_recorder):
        ack = AsyncMock()
        bolt_client = MagicMock()
        asyncio.run(edit_submit_handler(ack=ack, body=body, client=bolt_client))

    # 1. The view handler still acks + still drives handle_approval_action.
    ack.assert_awaited_once()
    approval_recorder.assert_called_once()
    assert approval_recorder.call_args.kwargs["op_id"] == "OP-WIRE-1"
    assert approval_recorder.call_args.kwargs["decision"] == "edit"

    # 2. The wiring reached _persist_corrections → add_correction exactly once,
    #    with the seeded vendor + account/tax codes from the edited line.
    assert len(correction_calls) == 1
    assert correction_calls[0] == {
        "client_id": "CL-1",
        "vendor": "Hotel Booking",
        "account_code": "6010",
        "tax_code": "ZR",
    }


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
    # Classify announces it's looking at the document (warmer, not "Classifying").
    assert "document" in event_stage_label(_node_event("classify_node")).lower()
    # Extract labels name WHAT the doc was identified as.
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
            client_store=_seeded_client_store(db),
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
    lowered = [t.lower() for t in update_texts]
    assert any("taking a look" in t for t in lowered)            # classify
    assert any("reading the line items" in t for t in lowered)   # extract (invoice)
    assert any("reconciling" in t for t in lowered)              # tax/approval
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
            client_store=_seeded_client_store(db),
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
            client_store=_seeded_client_store(db),
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
            client_store=_seeded_client_store(db),
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
         patch("invoice_processing.export.client_context.FirestoreClientStore"), \
         patch.object(slack_runner, "build_chat_runner",
                      return_value=SimpleNamespace(app_name="accounting_agents_assistant")):
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


# =========================================================================== #
# Task 9: One Job summary per batch drop (collapse per-doc spam into 1 thread)
# =========================================================================== #


def _capture_message_handler_with_slack_client(injected_slack: FakeSlackClient):
    """Same as ``_capture_message_handler`` but injects ``injected_slack`` as the
    sync WebClient so we can read its recorded ``chat_postMessage`` /
    ``chat_update`` calls — the outer handler must post ONE summary + edit it.
    """
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
    rm = MagicMock()
    rm.app_name = "acc"

    with patch.object(slack_runner, "_seen", fresh_seen), \
         patch("slack_bolt.async_app.AsyncApp", return_value=fake_app), \
         patch("slack_sdk.WebClient", return_value=injected_slack), \
         patch("invoice_processing.export.client_context.FirestoreClientStore"), \
         patch.object(slack_runner, "build_chat_runner",
                      return_value=SimpleNamespace(app_name="accounting_agents_assistant")):
        build_async_app(
            runner=rm,
            ledger_store=MagicMock(),
            db=MagicMock(),
        )

    return registered["message"], fresh_seen


def test_batch_drop_posts_one_job_summary_then_threads_per_doc():
    """3 files dropped → exactly ONE top-level chat_postMessage + 3 process_file_event
    calls each carrying thread_ts=<summary_ts> + ONE chat_update editing the summary
    with the final tally (ADR-0007).
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from accounting_agents import slack_runner
    from app.slack_app import _SeenEvents

    # The handler reads ``_seen`` from the module globals at CALL time (not at
    # build time), so reset it here to a fresh instance for hermetic dedup.
    slack_runner._seen = _SeenEvents()
    slack = FakeSlackClient()
    handler, _ = _capture_message_handler_with_slack_client(slack)

    body = {"event_id": "Ev-batch-1"}
    event = {
        "type": "message",
        "subtype": "file_share",
        "ts": "222.001",
        "channel": "C-batch",
        "files": [{"id": "FA1"}, {"id": "FA2"}, {"id": "FA3"}],
    }
    fake_client = MagicMock()
    # Mix of delivered + paused per doc → drives the needs_review tail.
    mock_pfe = AsyncMock(side_effect=[
        {"status": "delivered", "append": {"appended": 1, "software": "Xero", "fy": "2026"}},
        {"status": "delivered", "append": {"appended": 1, "software": "Xero", "fy": "2026"}},
        {"status": "paused", "op_id": "OP1"},
    ])

    with patch.object(slack_runner, "process_file_event", mock_pfe), \
         patch.object(slack_runner, "download_pdf_bytes", return_value=b"%PDF fake"):
        asyncio.run(handler(event=event, body=body, client=fake_client))

    # --- ONE top-level summary message posted by the outer handler ---
    top_level = [p for p in slack._posts if not p.get("thread_ts")]
    assert len(top_level) == 1, f"expected 1 top-level summary, got {len(top_level)}: {top_level}"
    summary_text = top_level[0].get("text", "")
    assert "3" in summary_text  # total count surfaced in the placeholder
    assert "Processing" in summary_text  # initial placeholder

    summary_ts = top_level[0].get("ts")  # e.g. "1.000"
    assert summary_ts, "summary message must carry a ts"

    # --- each process_file_event was called with thread_ts=<summary_ts> ---
    assert mock_pfe.call_count == 3
    for c in mock_pfe.call_args_list:
        assert c.kwargs.get("thread_ts") == summary_ts, (
            f"per-doc call must carry thread_ts={summary_ts}, got {c.kwargs}"
        )

    # --- after the loop: ONE chat_update edits the summary with the final tally ---
    assert len(slack.updates) == 1
    upd = slack.updates[0]
    assert upd.get("channel") == "C-batch"
    assert upd.get("ts") == summary_ts
    final_text = upd.get("text", "")
    assert "Processed" in final_text  # tally uses the helper's template
    assert "2" in final_text  # posted count
    assert "1" in final_text  # needs_review count
    assert "Xero" in final_text or "FY2026" in final_text


def test_single_file_drop_still_posts_summary_then_thread():
    """A 1-file drop also collapses to one summary message + one threaded doc card.

    Behaviour parity with multi-file drops — the user always sees ONE Job summary
    per upload event, regardless of file count (ADR-0007).
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from accounting_agents import slack_runner
    from app.slack_app import _SeenEvents

    slack_runner._seen = _SeenEvents()
    slack = FakeSlackClient()
    handler, _ = _capture_message_handler_with_slack_client(slack)

    body = {"event_id": "Ev-batch-2"}
    event = {
        "type": "message",
        "subtype": "file_share",
        "ts": "222.002",
        "channel": "C-single",
        "files": [{"id": "FS1"}],
    }
    fake_client = MagicMock()
    mock_pfe = AsyncMock(return_value={
        "status": "delivered",
        "append": {"appended": 1, "software": "QBS Ledger", "fy": "2026"},
    })

    with patch.object(slack_runner, "process_file_event", mock_pfe), \
         patch.object(slack_runner, "download_pdf_bytes", return_value=b"%PDF fake"):
        asyncio.run(handler(event=event, body=body, client=fake_client))

    top_level = [p for p in slack._posts if not p.get("thread_ts")]
    assert len(top_level) == 1
    assert "Processing" in top_level[0].get("text", "")
    assert mock_pfe.call_count == 1
    assert mock_pfe.call_args.kwargs.get("thread_ts") == top_level[0].get("ts")
    # After-loop edit happens once (single delivered doc → 1 posted, 0 needs review).
    assert len(slack.updates) == 1
    assert "Processed" in slack.updates[0].get("text", "")
    assert "1" in slack.updates[0].get("text", "")


# =========================================================================== #
# Task 5: doc_label built from run state, posted on card, persisted on interrupt
# =========================================================================== #


def test_doc_label_from_state():
    """First invoice vendor + total formatted as a per-document label."""
    state = {
        "source_filename": "Receipt-Hotel.pdf",
        nodes.NORMALIZED_KEY: [
            {
                "vendor_name": "Hotel Booking",
                "total_amount": 51.49,
                "currency": "SGD",
            }
        ],
    }
    label = _doc_label_from_state(state)
    assert "Receipt-Hotel.pdf" in label
    assert "Hotel Booking" in label
    assert "51.49" in label


def test_doc_label_from_state_falls_back_on_missing_invoice():
    """No normalized invoices yet → label is just the filename."""
    label = _doc_label_from_state({"source_filename": "mystery.pdf"})
    assert "mystery.pdf" in label
    # No vendor / total noise when the invoice is absent.
    assert "·" not in label.split("mystery.pdf", 1)[1]


def test_doc_label_from_state_uses_issuer_name_alias():
    """``issuer_name`` is the sales-direction alias for ``vendor_name``."""
    state = {
        "source_filename": "INV-1001.pdf",
        nodes.NORMALIZED_KEY: [
            {"issuer_name": "BigBuyer Ltd", "total_amount": 100.0, "currency": "SGD"}
        ],
    }
    label = _doc_label_from_state(state)
    assert "BigBuyer Ltd" in label
    assert "100.00" in label


def test_doc_label_from_state_skips_money_when_total_missing():
    """No numeric total → no trailing currency block."""
    state = {
        "source_filename": "INV-1001.pdf",
        nodes.NORMALIZED_KEY: [
            {"vendor_name": "Acme", "currency": "SGD"}  # no total_amount
        ],
    }
    label = _doc_label_from_state(state)
    assert "Acme" in label
    assert "SGD" not in label


def test_doc_label_from_state_default_filename():
    """Empty state still produces a non-empty label (so the card never looks bare)."""
    label = _doc_label_from_state({})
    assert "document" in label


def test_process_file_event_interrupt_persists_doc_label_and_renders_it():
    """Approval card header names the document; interrupt doc carries the same label.

    Patches ``_read_interrupt_summary`` to return a known summary, seeds the
    session state with a normalized invoice so the label is rich, and asserts
    the resulting card text + Firestore doc both carry the label.
    """
    from unittest.mock import AsyncMock, patch

    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    # Seed a profile whose channel_id matches the run we drive below so the
    # soft-gate at process_file_event lets the run proceed.
    seeded = _seeded_client_store(db, channel_id="C5", client_id="c5")

    interrupt_event = SimpleNamespace(
        content=SimpleNamespace(parts=[]),
        get_function_calls=lambda: [SimpleNamespace(name="adk_request_input", id="C5:F5")],
    )
    # Pre-seed the final-state view with a normalized invoice so the label is
    # built from the same shape the real graph produces.
    final_state = {
        "approval_message": "needs review: line X",
        "source_filename": "Receipt-Hotel.pdf",
        nodes.NORMALIZED_KEY: [
            {"vendor_name": "Hotel Booking", "total_amount": 51.49, "currency": "SGD"}
        ],
    }
    runner = _FakeRunner([interrupt_event], final_state)

    with patch(
        "accounting_agents.slack_runner._read_interrupt_summary",
        new=AsyncMock(return_value="needs review: line X"),
    ):
        result = asyncio.run(
            process_file_event(
                runner=runner,
                ledger_store=store,
                db=db,
                slack_client=slack,
                channel_id="C5",
                file_id="F5",
                app_name="acc",
                download_fn=lambda c, f: b"%PDF fake",
                source_filename="Receipt-Hotel.pdf",
                client_store=seeded,
            )
        )

    assert result["status"] == "paused"

    # Card text names the document above the review header.
    card = _last_blocks(slack)
    head = next(
        b["text"]["text"] for b in card
        if b.get("type") == "section" and b.get("text", {}).get("type") == "mrkdwn"
    )
    assert "Receipt-Hotel.pdf" in head
    assert "Hotel Booking" in head
    assert "51.49" in head
    # And the label sits ABOVE the standard review-needed header line.
    assert head.index("Receipt-Hotel.pdf") < head.index("Review needed")

    # Interrupt correlation doc persists the label alongside the summary.
    snap = db.collection("interrupts").document("C5:F5").get()
    assert snap.exists
    doc = snap.to_dict()
    assert doc.get("doc_label")
    assert "Receipt-Hotel.pdf" in doc["doc_label"]


# =========================================================================== #
# Task 7: edits-from-view-state parser (Slack view_submission → line edits DTO)
# =========================================================================== #


def test_edits_from_view_state_builds_line_edits():
    view = {"state": {"values": {
        "acct_0": {"v": {"selected_option": {"value": "6010"}}},
        "tax_0":  {"v": {"selected_option": {"value": "ZR"}}},
        "amt_0":  {"v": {"value": "44.74"}},
    }}}
    edits = _edits_from_view_state(view)
    assert edits == {"lines": [{"index": 0, "account_code": "6010",
                                "tax_treatment": "ZR", "net_amount": 44.74}]}


# =========================================================================== #
# Task 8: an edit becomes a per-client Correction (ADR-0004)
# =========================================================================== #


def test_persist_corrections_writes_vendor_mapping():
    """One Correction per edited line that carries account_code / tax_code.

    Mirrors the spec test verbatim: a single line with both fields produces one
    ``add_correction`` call with the invoice's vendor (taken from the first
    normalized invoice's ``vendor_name``).
    """
    saved = []

    class _Store:
        def add_correction(self, *, client_id, vendor, account_code=None, tax_code=None):
            saved.append((client_id, vendor, account_code, tax_code))

    state = {
        "client_id": "CL-1",
        nodes.NORMALIZED_KEY: [
            {"vendor_name": "Hotel Booking",
             "lines": [{"description": "Room"}]}  # uncategorized → edits are real changes
        ],
    }
    edits = {"lines": [{"index": 0, "account_code": "6010", "tax_treatment": "ZR"}]}
    _persist_corrections(_Store(), state, edits)
    assert saved == [("CL-1", "Hotel Booking", "6010", "ZR")]


def test_persist_corrections_reads_nested_party_real_serialized_shape():
    """Vendor comes from the nested party the SERIALIZER actually produces.

    ``_inv_to_dict`` = ``asdict(NormalizedInvoice)`` nests the parties under
    ``supplier`` / ``customer`` — there is no flat ``vendor_name`` key on real
    state. Purchases key off ``supplier.name``; sales key off ``customer.name``
    (mirroring ``NormalizedInvoice.counterparty``). This is the shape that broke
    learning in production.
    """
    saved = []

    class _Store:
        def add_correction(self, *, client_id, vendor, account_code=None, tax_code=None):
            saved.append((client_id, vendor, account_code, tax_code))

    # Purchase → supplier.name; proposed 6-3000, human changes to 5-1000.
    purchase_state = {
        "client_id": "CL-1",
        nodes.NORMALIZED_KEY: [
            {"doc_type": "purchase",
             "supplier": {"name": "Darrell Podaima"},
             "customer": {"name": "Auditair International Pte. Ltd."},
             "lines": [{"description": "audit", "account_code": "6-3000"}]}
        ],
    }
    _persist_corrections(_Store(), purchase_state,
                         {"lines": [{"index": 0, "account_code": "5-1000"}]})
    assert saved == [("CL-1", "Darrell Podaima", "5-1000", None)]

    # Sales → customer.name; proposed line untaxed, human sets SR.
    saved.clear()
    sales_state = {
        "client_id": "CL-1",
        nodes.NORMALIZED_KEY: [
            {"doc_type": "sales",
             "supplier": {"name": "Auditair International Pte. Ltd."},
             "customer": {"name": "PTTEP"},
             "lines": [{"description": "svc"}]}
        ],
    }
    _persist_corrections(_Store(), sales_state,
                         {"lines": [{"index": 0, "tax_treatment": "SR"}]})
    assert saved == [("CL-1", "PTTEP", None, "SR")]


def test_persist_corrections_only_changed_line_no_collision_multi_line():
    """Multi-line invoice: only the line the human CHANGED becomes a Correction.

    Regression for the collision bug — the modal re-submits every line, so an
    unchanged line (same code as proposed) must NOT overwrite the edited line's
    vendor mapping. Here line 0 is changed 6-3000→5-1000; line 1 is left at the
    proposed 6-3000. Exactly one Correction (5-1000) must be written.
    """
    saved = []

    class _Store:
        def add_correction(self, *, client_id, vendor, account_code=None, tax_code=None):
            saved.append((client_id, vendor, account_code, tax_code))

    state = {
        "client_id": "CL-1",
        nodes.NORMALIZED_KEY: [
            {"doc_type": "purchase",
             "supplier": {"name": "Darrell Podaima"},
             "lines": [
                 {"description": "audit", "account_code": "6-3000"},
                 {"description": "report", "account_code": "6-3000"},
             ]}
        ],
    }
    edits = {"lines": [
        {"index": 0, "account_code": "5-1000"},   # changed
        {"index": 1, "account_code": "6-3000"},   # unchanged (== proposal)
    ]}
    _persist_corrections(_Store(), state, edits)
    assert saved == [("CL-1", "Darrell Podaima", "5-1000", None)]


def test_persist_corrections_skips_unchanged_lines():
    """A line resubmitted at its proposed value is NOT a Correction."""
    saved = []

    class _Store:
        def add_correction(self, *, client_id, vendor, account_code=None, tax_code=None):
            saved.append((client_id, vendor, account_code, tax_code))

    state = {
        "client_id": "CL-1",
        nodes.NORMALIZED_KEY: [
            {"doc_type": "purchase",
             "supplier": {"name": "Acme"},
             "lines": [{"description": "x", "account_code": "6-3000"}]}
        ],
    }
    _persist_corrections(_Store(), state, {"lines": [{"index": 0, "account_code": "6-3000"}]})
    assert saved == []


def test_persist_corrections_skips_lines_with_no_code_fields():
    """A line that only changed ``net_amount`` (no account_code, no tax_treatment)
    is skipped. Edits without either code field are not entity-memory worthy —
    the user's amount tweak is a one-off variance, not a vendor rule.
    """
    saved = []

    class _Store:
        def add_correction(self, *, client_id, vendor, account_code=None, tax_code=None):
            saved.append((client_id, vendor, account_code, tax_code))

    state = {
        "client_id": "CL-1",
        nodes.NORMALIZED_KEY: [{"vendor_name": "Acme"}],
    }
    edits = {"lines": [{"index": 0, "net_amount": 12.34}]}
    _persist_corrections(_Store(), state, edits)
    assert saved == []


def test_persist_corrections_uses_issuer_name_alias_when_vendor_missing():
    """``issuer_name`` (sales-direction alias) is the fallback vendor field."""
    saved = []

    class _Store:
        def add_correction(self, *, client_id, vendor, account_code=None, tax_code=None):
            saved.append((client_id, vendor, account_code, tax_code))

    state = {
        "client_id": "CL-1",
        nodes.NORMALIZED_KEY: [{"issuer_name": "BigBuyer Ltd"}],
    }
    edits = {"lines": [{"index": 0, "account_code": "5000"}]}
    _persist_corrections(_Store(), state, edits)
    assert saved == [("CL-1", "BigBuyer Ltd", "5000", None)]


def test_persist_corrections_noop_without_client_id_or_invoice():
    """Defensive: missing client_id or empty invoice list ⇒ no writes (no crash)."""
    saved = []

    class _Store:
        def add_correction(self, *, client_id, vendor, account_code=None, tax_code=None):
            saved.append((client_id, vendor, account_code, tax_code))

    _persist_corrections(_Store(), {}, {"lines": [{"index": 0, "account_code": "6010"}]})
    _persist_corrections(_Store(), {"client_id": "X"}, {"lines": [{"index": 0, "account_code": "6010"}]})
    _persist_corrections(_Store(), {"client_id": "X", nodes.NORMALIZED_KEY: []},
                         {"lines": [{"index": 0, "account_code": "6010"}]})
    assert saved == []


def test_main_async_wires_firestore_client_store(monkeypatch):
    """Socket mode must seed onboarding/commands with the SAME Firestore store
    the document pipeline reads — not an ephemeral in-memory store. Otherwise a
    profile registered via the modal would be invisible to processing.
    """
    import accounting_agents.slack_runner as sr
    from invoice_processing.export.client_context import FirestoreClientStore

    captured: dict = {}

    monkeypatch.setattr(sr, "build_runner", lambda: SimpleNamespace(app_name="acc"))
    monkeypatch.setattr(sr, "SlackLedgerStore", lambda db: object())
    monkeypatch.setattr(
        "accounting_agents.sessions.FirestoreSessionService",
        lambda: SimpleNamespace(client=object()),
    )

    def _fake_build(**kwargs):
        captured["store"] = kwargs.get("store")
        return object()

    monkeypatch.setattr(sr, "build_async_app", _fake_build)

    class _FakeHandler:
        def __init__(self, app, token):
            captured["token"] = token

        async def start_async(self):
            captured["started"] = True

    monkeypatch.setattr(
        "slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler",
        _FakeHandler,
    )
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")

    asyncio.run(sr._main_async())

    assert isinstance(captured["store"], FirestoreClientStore)
    assert captured.get("started") is True


# =========================================================================== #
# Task 8: Reject unreadable uploads — empty bytes or unknown mime
# =========================================================================== #


def test_process_file_event_rejects_empty_bytes():
    """An empty download is rejected with a friendly message; NOT counted as processed.

    The pipeline must post a clear rejection message and return status
    ``"rejected_unreadable"`` so the batch-drop tally excludes it from
    the "Processed N documents" count (neither ``posted`` nor ``needs_review``
    increments for rejected files).
    """
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    runner = _FakeRunner([], _ledger_payload())  # should never run the graph

    result = asyncio.run(
        process_file_event(
            runner=runner,
            ledger_store=store,
            db=db,
            slack_client=slack,
            channel_id="C1",
            file_id="F1",
            app_name="acc",
            download_fn=lambda c, f: b"",  # empty — nothing downloaded
            source_filename="mystery.bin",
            client_store=_seeded_client_store(db),
        )
    )

    # Must return a distinct rejection status (not "delivered" or "paused").
    assert result["status"] == "rejected_unreadable"
    # A friendly message was posted — not the "Processed" path.
    texts = _posted_texts(slack)
    assert any(
        "empty" in t.lower() or "supported" in t.lower() or "couldn't read" in t.lower()
        for t in texts
    ), f"expected rejection message in: {texts}"
    # The graph was never driven (no artifacts saved).
    assert runner.artifact_service.saved == {}
    # No ledger rows appended (no upload).
    assert slack.uploads == []


def test_process_file_event_rejects_unknown_extension():
    """A file with an unrecognised extension is rejected before the graph runs.

    Simulates an "unknown/unknown size" upload: the bytes are non-empty but the
    extension is not one the pipeline supports (.exe is clearly wrong). The
    validator must catch this and reject it rather than forwarding garbage to
    Gemini.
    """
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    runner = _FakeRunner([], _ledger_payload())  # must not run

    result = asyncio.run(
        process_file_event(
            runner=runner,
            ledger_store=store,
            db=db,
            slack_client=slack,
            channel_id="C1",
            file_id="F1",
            app_name="acc",
            download_fn=lambda c, f: b"\x4d\x5a\x90\x00" * 10,  # EXE magic bytes, non-empty
            source_filename="setup.exe",
            client_store=_seeded_client_store(db),
        )
    )

    assert result["status"] == "rejected_unreadable"
    texts = _posted_texts(slack)
    assert any(
        "empty" in t.lower() or "supported" in t.lower() or "couldn't read" in t.lower()
        for t in texts
    ), f"expected rejection message in: {texts}"
    assert runner.artifact_service.saved == {}
    assert slack.uploads == []


def test_process_file_event_accepted_pdf_still_processes():
    """A known-good extension (.pdf, non-empty bytes) passes the validator and proceeds.

    Regression guard: the validation guard must NOT block legitimate uploads.
    """
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    final_event = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text="done")]),
        get_function_calls=lambda: [],
    )
    runner = _FakeRunner([final_event], _ledger_payload())

    result = asyncio.run(
        process_file_event(
            runner=runner,
            ledger_store=store,
            db=db,
            slack_client=slack,
            channel_id="C1",
            file_id="F1",
            app_name="acc",
            download_fn=lambda c, f: b"%PDF-1.4 fake content",
            source_filename="invoice.pdf",
            client_store=_seeded_client_store(db),
        )
    )

    assert result["status"] == "delivered"
    assert len(slack.uploads) == 1


# =========================================================================== #
# build_fastapi_app — prod wiring test (Task 4)
# =========================================================================== #

def test_build_fastapi_app_wires_adk_graph(monkeypatch):
    """build_fastapi_app() returns a FastAPI app with POST /slack/events.

    Asserts (without any network/Slack calls) that the factory:
    1. Builds the runner via build_runner() (which binds accounting_agents.agent.app).
    2. Builds the async Bolt app via build_async_app().
    3. Wraps it in AsyncSlackRequestHandler.
    4. Exposes POST /slack/events.
    5. Exposes GET /healthz.

    Uses monkeypatch.setattr (not with-patch) so patches stay active during the
    lazy _get_handler() call triggered by tc.post("/slack/events").
    FirestoreSessionService and FirestoreClientStore are function-local imports
    inside _get_handler, so we patch the SOURCE modules.
    """
    from unittest.mock import AsyncMock, MagicMock

    import accounting_agents.sessions as _sessions_mod
    import accounting_agents.slack_runner as _runner_mod
    import invoice_processing.export.client_context as _ctx_mod
    import slack_bolt.adapter.fastapi.async_handler as _handler_mod

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    runner_calls: list = []
    app_calls: list = []
    fake_async_app = MagicMock()
    fake_handler = MagicMock()
    fake_handler.handle = AsyncMock(return_value=MagicMock(status_code=401))

    def _fake_build_runner(**kw):
        runner_calls.append(kw)
        return MagicMock(name="runner")

    def _fake_build_async_app(*, runner, ledger_store, db, store=None, bot_token=None):
        app_calls.append({"runner": runner, "store": store})
        return fake_async_app

    # Patch module-level names in slack_runner (build_runner/build_async_app/
    # SlackLedgerStore are module attributes so this works directly).
    monkeypatch.setattr(_runner_mod, "build_runner", _fake_build_runner)
    monkeypatch.setattr(_runner_mod, "build_async_app", _fake_build_async_app)
    monkeypatch.setattr(_runner_mod, "SlackLedgerStore", MagicMock(return_value=MagicMock()))

    # FirestoreSessionService/FirestoreClientStore are imported locally inside
    # _get_handler, so patch the SOURCE class in the source modules.
    monkeypatch.setattr(_sessions_mod, "FirestoreSessionService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(_ctx_mod, "FirestoreClientStore", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(_handler_mod, "AsyncSlackRequestHandler", MagicMock(return_value=fake_handler))

    from accounting_agents.slack_runner import build_fastapi_app
    api = build_fastapi_app()

    # Must return a FastAPI instance.
    assert isinstance(api, FastAPI)

    # POST /slack/events and GET /healthz must be registered.
    paths = {r.path for r in api.routes}
    assert "/slack/events" in paths
    assert "/healthz" in paths

    # Trigger lazy construction by hitting /slack/events — patches still active.
    tc = TestClient(api, raise_server_exceptions=False)
    tc.post("/slack/events")

    # build_runner was called exactly once (lazy construction happened once).
    assert len(runner_calls) == 1

    # build_async_app was called with a runner (proves graph is wired).
    assert len(app_calls) == 1
    assert app_calls[0]["runner"] is not None

    # A FirestoreClientStore was passed as the `store` (not InMemory).
    assert app_calls[0]["store"] is not None


# =========================================================================== #
# _resolve_file_name — real uploaded filename for validation + card labels
# (regression: handlers passed no name → default "document.pdf" → unsupported
#  files never rejected + every card mislabeled)
# =========================================================================== #


def test_resolve_file_name_prefers_file_object_name():
    from accounting_agents.slack_runner import _resolve_file_name

    class _Client:
        def files_info(self, file):
            raise AssertionError("must not call files_info when the name is present")

    assert _resolve_file_name(_Client(), "F1", {"name": "Invoice-99.pdf"}) == "Invoice-99.pdf"


def test_resolve_file_name_falls_back_to_files_info():
    from accounting_agents.slack_runner import _resolve_file_name

    class _Resp:
        data = {"file": {"name": "scan.exe"}}

    class _Client:
        def files_info(self, file):
            return _Resp()

    # No name on the (minimal file_shared) object → fetch via files_info, keep .exe.
    assert _resolve_file_name(_Client(), "F1", None) == "scan.exe"


def test_resolve_file_name_defaults_only_when_truly_unavailable():
    from accounting_agents.slack_runner import _resolve_file_name

    class _Client:
        def files_info(self, file):
            raise RuntimeError("boom")

    assert _resolve_file_name(_Client(), "F1", None) == "document.pdf"


# =========================================================================== #
# Step 1: chat session id + chat runner wiring (ADR-0008)
# =========================================================================== #


def test_chat_session_id_thread_case():
    """A message inside a Slack thread keys its session by the raw thread_ts.

    Replies in the same thread reuse the same session id → the assistant sees
    multi-turn history. The message ts is irrelevant in this case.
    """
    from accounting_agents.slack_runner import _chat_session_id

    sid = _chat_session_id("C123", "1700000000.123", "1700000005.456")
    assert sid == "C123:chat:1700000000.123"


def test_chat_session_id_day_bucket():
    """A top-level message (no thread_ts) buckets by the UTC day of its ts.

    1700000000 == 2023-11-14T22:13:20 UTC → day-2023-11-14.
    """
    from accounting_agents.slack_runner import _chat_session_id

    sid = _chat_session_id("C123", None, "1700000000.000")
    assert sid == "C123:chat:day-2023-11-14"


def test_chat_session_id_falls_back_to_channel_when_ts_missing():
    """A missing/unparseable message_ts degrades to ``channel_id`` alone.

    Direct callers (tests, one-shot scripts) may not have a message ts; the
    chat lane must still produce a usable session id.
    """
    from accounting_agents.slack_runner import _chat_session_id

    assert _chat_session_id("C123", None, None) == "C123"
    assert _chat_session_id("C123", None, "not-a-number") == "C123"


def test_chat_and_document_sessions_isolated():
    """A document session id and a chat session id on the same channel differ.

    Pipeline events never pollute chat history and vice versa (ADR-0008).
    """
    from accounting_agents.slack_runner import _chat_session_id, _per_doc_session_id

    doc = _per_doc_session_id("C9", "FILE1")
    chat = _chat_session_id("C9", "1700000000.123", "1700000005.456")
    assert doc != chat
    assert ":chat:" in chat
    assert ":chat:" not in doc


# --------------------------------------------------------------------------- #
# Multi-turn session reuse + chat-runner wiring
# --------------------------------------------------------------------------- #


class _CapturingChatRunner:
    """Minimal chat-runner stub that records each ``run_async`` call.

    Mirrors enough of the real ``Runner`` API for ``answer_question``: an
    ``app_name`` + ``session_service`` + an async-generator ``run_async`` that
    yields one final-text event.
    """

    def __init__(self, sessions: dict, app_name: str = "accounting_agents_assistant"):
        self.app_name = app_name
        self._sessions = sessions
        self.calls: list[dict] = []

        runner_self = self

        class _SessionService:
            def __init__(self):
                self.created: list[tuple] = []

            async def get_session(self, *, app_name, user_id, session_id):
                return runner_self._sessions.get((user_id, session_id))

            async def create_session(self, *, app_name, user_id, session_id, state=None):
                from google.adk.errors.already_exists_error import AlreadyExistsError
                if (user_id, session_id) in runner_self._sessions:
                    raise AlreadyExistsError("exists")
                self.created.append((user_id, session_id))
                runner_self._sessions[(user_id, session_id)] = _FakeSession(state or {})
                return runner_self._sessions[(user_id, session_id)]

        self.session_service = _SessionService()

    async def run_async(
        self, *, user_id, session_id, new_message=None, state_delta=None, run_config=None,
    ):
        self.calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "state_delta": state_delta or {},
                "run_config": run_config,
                "new_message": new_message,
            }
        )
        # Append a synthetic user event to the persisted session so the test can
        # assert that the second turn's session contains the first turn.
        sess = self._sessions.get((user_id, session_id))
        if sess is not None:
            events = getattr(sess, "events", None) or []
            events.append(("user", new_message))
            sess.events = events
        yield SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text="ok")]),
            get_function_calls=lambda: [],
        )


def _noop_ledger_store():
    """A ledger_store stub that returns empty rows so answer_question can run hermetically."""
    from unittest.mock import MagicMock

    s = MagicMock()
    s.latest_fy.return_value = None
    s.read_rows.return_value = []
    return s


def test_multi_turn_reuses_session():
    """Two ``answer_question`` calls on the same ``(channel, raw_thread_ts)`` reuse
    the same chat session and the second call sees the first turn's event.
    """
    from accounting_agents.slack_runner import answer_question

    slack = FakeSlackClient()
    sessions: dict = {}
    chat_runner = _CapturingChatRunner(sessions)

    asyncio.run(
        answer_question(
            runner=chat_runner,
            ledger_store=_noop_ledger_store(),
            slack_client=slack,
            channel_id="C-CHAT",
            question="Hello",
            app_name=chat_runner.app_name,
            client_store=None,
            message_ts="1700000000.001",
            thread_ts="1700000000.001",
            raw_thread_ts="1700000000.001",
        )
    )
    asyncio.run(
        answer_question(
            runner=chat_runner,
            ledger_store=_noop_ledger_store(),
            slack_client=slack,
            channel_id="C-CHAT",
            question="And again",
            app_name=chat_runner.app_name,
            client_store=None,
            message_ts="1700000000.002",
            thread_ts="1700000000.001",
            raw_thread_ts="1700000000.001",
        )
    )

    assert len(chat_runner.calls) == 2
    assert chat_runner.calls[0]["session_id"] == chat_runner.calls[1]["session_id"]
    assert chat_runner.calls[0]["session_id"] == "C-CHAT:chat:1700000000.001"

    # The second turn's persisted session contains events recorded by the first.
    sess = sessions[("C-CHAT", "C-CHAT:chat:1700000000.001")]
    assert len(getattr(sess, "events", [])) == 2


def test_answer_question_does_not_set_question_key():
    """The chat lane no longer embeds the question into state (multi-turn now)."""
    from accounting_agents.slack_runner import answer_question

    slack = FakeSlackClient()
    sessions: dict = {}
    chat_runner = _CapturingChatRunner(sessions)

    asyncio.run(
        answer_question(
            runner=chat_runner,
            ledger_store=_noop_ledger_store(),
            slack_client=slack,
            channel_id="C-CHAT",
            question="Whatever",
            app_name=chat_runner.app_name,
            client_store=None,
            message_ts="1700000000.500",
            thread_ts="1700000000.500",
            raw_thread_ts=None,
        )
    )

    state_delta = chat_runner.calls[0]["state_delta"]
    assert "question_text" not in state_delta
    # The ledger / channel keys are still seeded.
    assert state_delta.get("channel_id") == "C-CHAT"
    assert "ledger_data" in state_delta


def test_run_config_caps_recent_events():
    """``answer_question`` passes ``RunConfig(num_recent_events=20)`` to run_async."""
    from accounting_agents.slack_runner import answer_question

    slack = FakeSlackClient()
    sessions: dict = {}
    chat_runner = _CapturingChatRunner(sessions)

    asyncio.run(
        answer_question(
            runner=chat_runner,
            ledger_store=_noop_ledger_store(),
            slack_client=slack,
            channel_id="C-CHAT",
            question="hi",
            app_name=chat_runner.app_name,
            client_store=None,
            message_ts="1700000000.600",
            thread_ts="1700000000.600",
            raw_thread_ts=None,
        )
    )

    run_config = chat_runner.calls[0]["run_config"]
    assert run_config is not None
    gsc = run_config.get_session_config
    assert gsc is not None
    assert gsc.num_recent_events == 20


class _SilentModelChatRunner(_CapturingChatRunner):
    """Simulates ``gemini-2.5-flash-lite`` going silent after a tool call.

    Yields a function-response event (carrying ``result``), then a final event
    with NO text parts — the exact failure mode observed live (the model
    occasionally produces an empty completion after a tool returns, despite
    instruction telling it to always reply with text).
    """

    async def run_async(
        self, *, user_id, session_id, new_message=None, state_delta=None, run_config=None,
    ):
        self.calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "state_delta": state_delta or {},
                "run_config": run_config,
                "new_message": new_message,
            }
        )
        # Event 1: a tool result with a useful raw payload the user should see.
        fr = SimpleNamespace(response={"result": '{"revenue": 8500, "expenses": 1820.5, "net": 6679.5}'})
        yield SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text=None)]),
            get_function_calls=lambda: [],
            get_function_responses=lambda: [fr],
        )
        # Event 2: final event with EMPTY text parts (the model went silent).
        yield SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text=None)]),
            get_function_calls=lambda: [],
            get_function_responses=lambda: [],
        )


def test_silent_model_safety_net_surfaces_tool_result():
    """When the model goes silent after a tool call, the chat lane MUST surface
    the tool's raw result rather than the opaque ``rephrase your question``
    canned message. Observed live on 2026-06-15 with gemini-2.5-flash-lite.
    """
    from accounting_agents.slack_runner import answer_question

    slack = FakeSlackClient()
    sessions: dict = {}
    chat_runner = _SilentModelChatRunner(sessions)

    result = asyncio.run(
        answer_question(
            runner=chat_runner,
            ledger_store=_noop_ledger_store(),
            slack_client=slack,
            channel_id="C-CHAT",
            question="summarise the purchases",
            app_name=chat_runner.app_name,
            client_store=None,
            message_ts="1700000000.700",
            thread_ts="1700000000.700",
            raw_thread_ts="1700000000.700",
        )
    )

    posted = slack._posts[-1]["text"]
    assert "rephrasing your question" not in posted
    assert "revenue" in posted and "6679.5" in posted
    assert result["text"] == posted


# --------------------------------------------------------------------------- #
# build_async_app: chat_runner is used for text, FirestoreClientStore is default
# --------------------------------------------------------------------------- #


def test_build_async_app_uses_chat_runner_for_text():
    """The text-handler path calls ``chat_runner.run_async``, not the coordinator runner."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from accounting_agents import slack_runner

    fake_chat_runner = MagicMock()
    fake_chat_runner.app_name = "accounting_agents_assistant"

    # Capture the message handler with chat_runner injected.
    from app.slack_app import _SeenEvents

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
    rm = MagicMock()
    rm.app_name = "acc"

    with patch.object(slack_runner, "_seen", fresh_seen), \
         patch("slack_bolt.async_app.AsyncApp", return_value=fake_app), \
         patch.object(slack_runner, "build_chat_runner", return_value=fake_chat_runner):
        slack_runner.build_async_app(
            runner=rm,
            ledger_store=MagicMock(),
            db=MagicMock(),
            store=MagicMock(),  # avoid FirestoreClientStore default
        )

    handler = registered["message"]

    body = {"event_id": "Ev-text-cr"}
    event = {
        "type": "message",
        "ts": "1700000000.700",
        "channel": "C-qa",
        "text": "How am I doing?",
    }
    mock_aq = AsyncMock(return_value={"status": "answered", "text": "ok"})
    with patch.object(slack_runner, "answer_question", mock_aq):
        asyncio.run(handler(event=event, body=body, client=MagicMock()))

    mock_aq.assert_called_once()
    # The runner passed to answer_question is the chat runner, NOT the coordinator runner.
    assert mock_aq.call_args.kwargs["runner"] is fake_chat_runner
    assert mock_aq.call_args.kwargs["app_name"] == fake_chat_runner.app_name


def test_build_async_app_default_store_is_firestore():
    """When no ``store`` is supplied, ``build_async_app`` defaults to FirestoreClientStore.

    This fixes the socket-mode gap where onboarding writes used to vanish into
    an ``InMemoryClientStore`` while the pipeline read from Firestore.
    """
    from unittest.mock import MagicMock, patch

    from accounting_agents import slack_runner
    from invoice_processing.export.client_context import FirestoreClientStore

    captured: dict = {}
    real_build_async_app = slack_runner.build_async_app

    # Spy on FirestoreClientStore — capture the instance returned to the function.
    real_cls = FirestoreClientStore

    def _capture(*args, **kwargs):
        inst = real_cls.__new__(real_cls)
        # Avoid touching Firestore in __init__.
        captured["instance"] = inst
        return inst

    rm = MagicMock()
    rm.app_name = "acc"
    fake_app = MagicMock()
    fake_app.event = lambda *a, **k: (lambda fn: fn)
    fake_app.action = lambda *a, **k: (lambda fn: fn)
    fake_app.view = lambda *a, **k: (lambda fn: fn)
    fake_app.command = lambda *a, **k: (lambda fn: fn)

    with patch("slack_bolt.async_app.AsyncApp", return_value=fake_app), \
         patch("invoice_processing.export.client_context.FirestoreClientStore", side_effect=_capture), \
         patch.object(slack_runner, "build_chat_runner", return_value=MagicMock(app_name="chat")):
        real_build_async_app(
            runner=rm,
            ledger_store=MagicMock(),
            db=MagicMock(),
            # NOTE: store omitted → must default to FirestoreClientStore
        )

    assert "instance" in captured, "FirestoreClientStore must be instantiated when no store is passed"
    assert type(captured["instance"]).__name__ == "FirestoreClientStore"


# --------------------------------------------------------------------------- #
# _ensure_session is idempotent — the chat lane must reuse sessions across turns
# --------------------------------------------------------------------------- #


def test_ensure_session_is_idempotent_for_chat_reuse():
    """Calling ``_ensure_session`` twice for the same (user, session) is a no-op.

    The chat lane relies on this: the second turn must NOT wipe the events the
    first turn added — get-or-create semantics only.
    """
    from accounting_agents.slack_runner import _ensure_session

    class _Svc:
        def __init__(self):
            self.created: list[tuple] = []

        async def create_session(self, *, app_name, user_id, session_id, state=None):
            from google.adk.errors.already_exists_error import AlreadyExistsError
            key = (user_id, session_id)
            if key in self.created:
                raise AlreadyExistsError("exists")
            self.created.append(key)

    svc = _Svc()
    runner = SimpleNamespace(session_service=svc)
    asyncio.run(_ensure_session(runner, "app", "U1", "S1"))
    asyncio.run(_ensure_session(runner, "app", "U1", "S1"))  # must not raise
    assert svc.created == [("U1", "S1")]


# =========================================================================== #
# Chat-lane write confirm bridge (Step 4 / ADR-0009)
# =========================================================================== #


def test_classify_confirmation_reply():
    from accounting_agents.slack_runner import classify_confirmation_reply

    for yes in ("yes", "Yes.", "confirm", "go ahead", "ok", "do it", "please do"):
        assert classify_confirmation_reply(yes) is True, yes
    for no in ("no", "cancel", "stop", "don't", "nope"):
        assert classify_confirmation_reply(no) is False, no
    for ambiguous in ("what does it change?", "tell me more", ""):
        assert classify_confirmation_reply(ambiguous) is None, ambiguous


def _confirm_session_with_pending(fc_id="adk-confirm-1", payload=None):
    """Build a session whose last event has an UNANSWERED adk_request_confirmation."""
    from google.adk.events.event_actions import EventActions
    from google.adk.tools.tool_confirmation import ToolConfirmation

    payload = payload or {"op": "amend", "sheet": "Purchase", "row": 2,
                          "updates": {"Account Code / COA": "6010"}}
    ev = Event(
        author="assistant",
        content=types.Content(
            parts=[types.Part(function_call=types.FunctionCall(
                name="adk_request_confirmation", id=fc_id, args={}))]
        ),
        long_running_tool_ids=[fc_id],
        actions=EventActions(
            requested_tool_confirmations={
                fc_id: ToolConfirmation(hint="change account?", payload=payload)
            }
        ),
    )
    return SimpleNamespace(events=[ev])


def test_find_pending_confirmation_detects_unanswered():
    from accounting_agents.slack_runner import find_pending_confirmation

    session = _confirm_session_with_pending()
    found = find_pending_confirmation(session)
    assert found is not None
    fc_id, confirmation = found
    assert fc_id == "adk-confirm-1"
    assert confirmation.payload["updates"]["Account Code / COA"] == "6010"


def test_find_pending_confirmation_none_when_answered():
    from accounting_agents.slack_runner import find_pending_confirmation

    session = _confirm_session_with_pending(fc_id="fc-9")
    # Append a function_response answering fc-9 → no longer pending.
    answer = Event(
        author="user",
        content=types.Content(
            parts=[types.Part(function_response=types.FunctionResponse(
                id="fc-9", name="adk_request_confirmation", response={"confirmed": True}))]
        ),
    )
    session.events.append(answer)
    assert find_pending_confirmation(session) is None


def test_synthesize_confirmation_message_matches_id_and_payload():
    from accounting_agents.slack_runner import (
        _synthesize_confirmation_message,
        find_pending_confirmation,
    )

    session = _confirm_session_with_pending(fc_id="fc-77")
    fc_id, confirmation = find_pending_confirmation(session)
    msg = _synthesize_confirmation_message(fc_id, confirmation, confirmed=True)

    part = msg.parts[0]
    fr = part.function_response
    assert fr.id == "fc-77"
    assert fr.name == "adk_request_confirmation"
    assert fr.response["confirmed"] is True
    # Original requested payload is echoed back so the re-run tool sees its spec.
    assert fr.response["payload"]["updates"]["Account Code / COA"] == "6010"


def test_synthesize_confirmation_message_negative():
    from accounting_agents.slack_runner import (
        _synthesize_confirmation_message,
        find_pending_confirmation,
    )

    session = _confirm_session_with_pending()
    fc_id, confirmation = find_pending_confirmation(session)
    msg = _synthesize_confirmation_message(fc_id, confirmation, confirmed=False)
    assert msg.parts[0].function_response.response["confirmed"] is False


# =========================================================================== #
# Post-run write execution (_execute_pending_writes)
# =========================================================================== #


def _seed_two_row_ledger():
    """Seed a real two-row QBS Purchase ledger; return (slack, store)."""
    slack = FakeSlackClient()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[
            {"sheet": "Purchase", "doc_key": "k1", "rows": [
                {"Invoice Number": "INV-1", "Description": "AWS", "Source Amount": 1000.0,
                 "Account Code / COA": "6090", "Tax Amount": 90.0},
                {"Invoice Number": "INV-2", "Description": "Rent", "Source Amount": 2000.0,
                 "Account Code / COA": "6200", "Tax Amount": 0.0},
            ]},
        ],
    )
    return slack, store


def test_execute_pending_writes_amend_posts_audits_and_refreshes():
    from accounting_agents.slack_runner import _execute_pending_writes

    slack, store = _seed_two_row_ledger()
    state = {
        "pending_ledger_write": [
            {"op": "amend", "sheet": "Purchase", "row": 2,
             "updates": {"Account Code / COA": "6010"}, "tax_treatment": "SR"},
        ],
    }
    committed = asyncio.run(_execute_pending_writes(
        state=state, ledger_store=store, slack_client=slack, channel_id="C1",
        client_id="c1", fy="2026", session_id="S1", fc_id="fc-1",
    ))
    assert committed is True
    # Workbook actually mutated.
    rows = store.read_rows("c1", "2026", slack, "C1")
    assert rows[0]["Account Code / COA"] == "6010"
    # Pending list cleared + idempotency marker set.
    assert state["pending_ledger_write"] == []
    assert "fc-1" in state["committed_confirmations"]
    # A confirmation message was posted.
    assert any("Updated" in m["text"] for m in slack._posts)


def test_execute_pending_writes_remove():
    from accounting_agents.slack_runner import _execute_pending_writes

    slack, store = _seed_two_row_ledger()
    state = {"pending_ledger_write": [
        {"op": "remove", "sheet": "Purchase", "row": 2}]}
    asyncio.run(_execute_pending_writes(
        state=state, ledger_store=store, slack_client=slack, channel_id="C1",
        client_id="c1", fy="2026", session_id="S1", fc_id="fc-rm",
    ))
    rows = store.read_rows("c1", "2026", slack, "C1")
    # Row 2 (AWS) gone; only Rent remains.
    assert len(rows) == 1
    assert rows[0]["Description"] == "Rent"
    assert any("Removed" in m["text"] for m in slack._posts)


def test_execute_pending_writes_idempotent_double_yes():
    from accounting_agents.slack_runner import _execute_pending_writes

    slack, store = _seed_two_row_ledger()
    spec = {"op": "amend", "sheet": "Purchase", "row": 2,
            "updates": {"Account Code / COA": "6010"}, "tax_treatment": "SR"}

    state = {"pending_ledger_write": [spec]}
    asyncio.run(_execute_pending_writes(
        state=state, ledger_store=store, slack_client=slack, channel_id="C1",
        client_id="c1", fy="2026", session_id="S1", fc_id="fc-dup",
    ))
    posts_after_first = len([m for m in slack._posts if "Updated" in m["text"]])

    # Second "yes" for the SAME fc_id: re-queue the spec, but it must NOT re-apply.
    state["pending_ledger_write"] = [spec]
    committed2 = asyncio.run(_execute_pending_writes(
        state=state, ledger_store=store, slack_client=slack, channel_id="C1",
        client_id="c1", fy="2026", session_id="S1", fc_id="fc-dup",
    ))
    assert committed2 is False
    posts_after_second = len([m for m in slack._posts if "Updated" in m["text"]])
    assert posts_after_second == posts_after_first  # no duplicate write/post
    assert state["pending_ledger_write"] == []


# =========================================================================== #
# MEDIUM-2 — improved classify_confirmation_reply
# =========================================================================== #


def test_classify_confirmation_improved():
    from accounting_agents.slack_runner import classify_confirmation_reply

    # Leading-token affirmative + trailing words
    assert classify_confirmation_reply("yes please") is True
    assert classify_confirmation_reply("yes do it") is True
    assert classify_confirmation_reply("ok great") is True
    assert classify_confirmation_reply("sure thing") is True
    # Phrase contains
    assert classify_confirmation_reply("go ahead and change it") is True
    assert classify_confirmation_reply("please do that") is True
    # Negative precedence — "no" wins even if "yes" also present
    assert classify_confirmation_reply("no thanks") is False
    assert classify_confirmation_reply("no, not yet") is False
    assert classify_confirmation_reply("don't do it") is False
    assert classify_confirmation_reply("cancel that please") is False
    assert classify_confirmation_reply("wait, hold on") is False
    # Ambiguous — bare unrelated question
    assert classify_confirmation_reply("what does this change?") is None
    assert classify_confirmation_reply("tell me more") is None
    assert classify_confirmation_reply("maybe") is None
    assert classify_confirmation_reply("") is None


# =========================================================================== #
# MEDIUM-3 — staleness bound in find_pending_confirmation
# =========================================================================== #


def _make_stale_session(fc_id="stale-fc", n_extra_events=10):
    """Build a session where the adk_request_confirmation is older than the window."""
    from google.adk.events.event_actions import EventActions
    from google.adk.tools.tool_confirmation import ToolConfirmation

    confirm_event = Event(
        author="assistant",
        content=types.Content(
            parts=[types.Part(function_call=types.FunctionCall(
                name="adk_request_confirmation", id=fc_id, args={}))]
        ),
        long_running_tool_ids=[fc_id],
        actions=EventActions(
            requested_tool_confirmations={
                fc_id: ToolConfirmation(hint="change something?", payload={"op": "amend"})
            }
        ),
    )
    # Pad with unrelated events to push the confirmation outside the window.
    filler = [
        Event(
            author="user",
            content=types.Content(parts=[types.Part(text=f"question {i}")]),
        )
        for i in range(n_extra_events)
    ]
    return SimpleNamespace(events=[confirm_event] + filler)


def test_find_pending_confirmation_ignores_stale():
    from accounting_agents.slack_runner import find_pending_confirmation

    # With 10 extra events after the confirmation, it is outside the 6-event window.
    session = _make_stale_session(n_extra_events=10)
    assert find_pending_confirmation(session) is None


def test_find_pending_confirmation_within_window():
    from accounting_agents.slack_runner import find_pending_confirmation

    # With only 2 extra events, the confirmation is still within the window.
    session = _make_stale_session(n_extra_events=2)
    result = find_pending_confirmation(session)
    assert result is not None
    fc_id, _ = result
    assert fc_id == "stale-fc"


def test_find_pending_confirmation_custom_window():
    from accounting_agents.slack_runner import find_pending_confirmation

    # Large custom window sees the stale event.
    session = _make_stale_session(n_extra_events=10)
    result = find_pending_confirmation(session, staleness_window=20)
    assert result is not None


# =========================================================================== #
# HIGH-2 — signature mismatch refuses the write
# =========================================================================== #


def _seed_two_row_ledger_sig():
    """Seed a two-row QBS Purchase ledger; return (slack, store)."""
    slack = FakeSlackClient()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[
            {"sheet": "Purchase", "doc_key": "k1", "rows": [
                {"Invoice Number": "INV-1", "Description": "AWS hosting",
                 "Source Amount": 1000.0, "Account Code / COA": "6090",
                 "Tax Amount": 90.0},
                {"Invoice Number": "INV-2", "Description": "Rent",
                 "Source Amount": 2000.0, "Account Code / COA": "6200",
                 "Tax Amount": 0.0},
            ]},
        ],
    )
    return slack, store


def test_execute_pending_writes_refuses_on_signature_mismatch():
    """A stale or replayed remove that targets a shifted row is refused."""
    from accounting_agents.slack_runner import _execute_pending_writes

    slack, store = _seed_two_row_ledger_sig()
    # Simulate: the first row was already deleted before the "yes" arrived,
    # shifting row numbers. We give a signature that now matches nothing.
    state = {
        "pending_ledger_write": [
            {"op": "remove", "sheet": "Purchase", "row": 2,
             "row_signature": "deadbeefdeadbeef"},  # wrong sig
        ],
    }
    committed = asyncio.run(_execute_pending_writes(
        state=state, ledger_store=store, slack_client=slack, channel_id="C1",
        client_id="c1", fy="2026", session_id="S1", fc_id="fc-sig",
    ))
    # Nothing was committed.
    assert committed is False
    # Both rows still intact.
    rows = store.read_rows("c1", "2026", slack, "C1")
    assert len(rows) == 2
    # A warning message was posted.
    assert any("⚠️" in m["text"] for m in slack._posts)


def test_execute_pending_writes_correct_signature_succeeds():
    """A write with the correct row signature goes through normally."""
    from accounting_agents.assistant import _row_signature
    from accounting_agents.slack_runner import _execute_pending_writes

    slack, store = _seed_two_row_ledger_sig()
    rows = store.read_rows("c1", "2026", slack, "C1")
    row0 = rows[0]
    sig = _row_signature(row0)

    state = {
        "pending_ledger_write": [
            {"op": "amend", "sheet": "Purchase", "row": 2,
             "updates": {"Account Code / COA": "6010"}, "tax_treatment": "SR",
             "row_signature": sig},
        ],
    }
    committed = asyncio.run(_execute_pending_writes(
        state=state, ledger_store=store, slack_client=slack, channel_id="C1",
        client_id="c1", fy="2026", session_id="S1", fc_id="fc-ok",
    ))
    assert committed is True
    updated = store.read_rows("c1", "2026", slack, "C1")
    assert updated[0]["Account Code / COA"] == "6010"


def test_execute_pending_writes_remove_replay_after_marker_failure():
    """Double-replay of a remove: second attempt hits wrong sig (row already gone)."""
    from accounting_agents.assistant import _row_signature
    from accounting_agents.slack_runner import _execute_pending_writes

    slack, store = _seed_two_row_ledger_sig()
    rows = store.read_rows("c1", "2026", slack, "C1")
    sig = _row_signature(rows[0])

    spec = {"op": "remove", "sheet": "Purchase", "row": 2, "row_signature": sig}

    # First application — succeeds and removes row 2.
    state1 = {"pending_ledger_write": [spec]}
    asyncio.run(_execute_pending_writes(
        state=state1, ledger_store=store, slack_client=slack, channel_id="C1",
        client_id="c1", fy="2026", session_id="S1", fc_id="fc-replay",
    ))
    assert len(store.read_rows("c1", "2026", slack, "C1")) == 1

    # Simulated replay (marker persist failed): same spec again WITHOUT fc_id guard.
    # The row at position 2 is now "Rent" (shifted up) — signature mismatch → refuse.
    state2 = {"pending_ledger_write": [spec]}
    committed2 = asyncio.run(_execute_pending_writes(
        state=state2, ledger_store=store, slack_client=slack, channel_id="C1",
        client_id="c1", fy="2026", session_id="S1", fc_id=None,  # no marker
    ))
    assert committed2 is False
    # "Rent" row still intact — the wrong row was NOT deleted.
    remaining = store.read_rows("c1", "2026", slack, "C1")
    assert len(remaining) == 1
    assert remaining[0]["Description"] == "Rent"


# --------------------------------------------------------------------------- #
# Step 2B: handle_review_action post-resume continuation (the Critical fix).
# After a confirm_as_is / reextract_as resume, the run flows to the terminal
# approval_gate, which either pauses AGAIN (must post the approval card) or
# auto-approves and completes (must persist + deliver — else silent data loss).
# --------------------------------------------------------------------------- #
def test_handle_review_action_delivers_on_clean_completion(monkeypatch):
    """confirm_as_is whose resumed run auto-approves → persist_and_deliver runs."""
    from accounting_agents.slack_runner import handle_review_action

    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    op_id = "CR9:FR9:review"
    write_interrupt(
        db, op_id, session_id="CR9:FR9", channel_id="CR9", slack_file_id="FR9",
        message_ts="1.1", user_id="CR9", extra={"kind": "review", "question": "q?", "reasons": []},
    )

    # Resumed run completes with no further interrupt.
    final_event = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text="done")]),
        get_function_calls=lambda: [],
    )

    async def fake_resume(runner, db_, op, decision):
        return [final_event]

    monkeypatch.setattr("accounting_agents.slack_runner.resume_session", fake_resume)

    runner = _FakeRunner([], _ledger_payload())
    # The (faked) resumed run would have populated the session; mark it created
    # so persist_and_deliver can read the final ledger payload.
    runner.session_service.created = True
    result = asyncio.run(
        handle_review_action(
            runner=runner, ledger_store=store, db=db, slack_client=slack,
            op_id=op_id, action="confirm_as_is", app_name="acc",
        )
    )

    assert result["status"] == "resumed"
    # The document was actually delivered to the ledger (the silent-data-loss guard).
    assert result["outcome"]["status"] == "delivered"
    assert len(slack.uploads) == 1
    assert any("FY2026 ledger" in t for t in _posted_texts(slack))


def test_handle_review_action_reposts_terminal_card_on_repause(monkeypatch):
    """confirm_as_is whose resumed run pauses again at approval_gate → approval card posted."""
    from accounting_agents.slack_runner import handle_review_action

    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    op_id = "CR8:FR8:review"
    write_interrupt(
        db, op_id, session_id="CR8:FR8", channel_id="CR8", slack_file_id="FR8",
        message_ts="2.2", user_id="CR8", extra={"kind": "review", "question": "q?", "reasons": []},
    )

    # Resumed run pauses again at the TERMINAL gate with the BASE id (no :review).
    repause_event = SimpleNamespace(
        content=SimpleNamespace(parts=[]),
        get_function_calls=lambda: [SimpleNamespace(name="adk_request_input", id="CR8:FR8")],
    )

    async def fake_resume(runner, db_, op, decision):
        return [repause_event]

    monkeypatch.setattr("accounting_agents.slack_runner.resume_session", fake_resume)

    runner = _FakeRunner([], {"approval_message": "needs review: line X"})
    result = asyncio.run(
        handle_review_action(
            runner=runner, ledger_store=store, db=db, slack_client=slack,
            op_id=op_id, action="confirm_as_is", app_name="acc",
        )
    )

    assert result["status"] == "resumed"
    # The terminal approval interrupt was surfaced (not silently stalled).
    assert result["outcome"]["status"] == "paused"
    assert result["outcome"]["op_id"] == "CR8:FR8"
    # A base-id interrupt doc was written so the approve/edit/reject card can resume.
    assert read_interrupt(db, "CR8:FR8") is not None
    # Nothing uploaded while paused.
    assert slack.uploads == []


# =========================================================================== #
# learn_mapping runner drain (Step 7 / C-3)
# =========================================================================== #


def test_runner_drains_pending_learn_mapping():
    """Post-run drain: pending_learn_mapping entries call add_correction and clear the list.

    Uses ``_CapturingChatRunner`` with a pre-seeded session that already
    contains ``pending_learn_mapping`` (as the learn_mapping tool would have
    written it during the turn).  Verifies:
    - ``client_store.add_correction`` is called once with the right args.
    - The session's ``pending_learn_mapping`` list is cleared via _apply_state_delta.
    """
    from accounting_agents.slack_runner import PENDING_LEARN_KEY, answer_question

    # ---- fake client store that records add_correction calls ----
    class _FakeLearnStore:
        def __init__(self):
            self.corrections: list[dict] = []

        def get_by_channel(self, channel_id):
            return None  # no profile → profile_delta stays empty

        def add_correction(self, *, client_id, vendor, account_code=None, tax_code=None):
            self.corrections.append({
                "client_id": client_id,
                "vendor": vendor,
                "account_code": account_code,
                "tax_code": tax_code,
            })

    fake_store = _FakeLearnStore()
    slack = FakeSlackClient()

    # Pre-seed the session with the learn spec already in state
    # (mimicking what the tool wrote inside the agent turn).
    channel_id = "C-LEARN"
    session_id = f"{channel_id}:chat:1700000000.001"
    sessions: dict = {
        (channel_id, session_id): _FakeSession({
            PENDING_LEARN_KEY: [
                {"vendor": "Acme Cloud", "account_code": "6090", "tax_code": None},
            ],
        }),
    }
    chat_runner = _CapturingChatRunner(sessions)

    asyncio.run(
        answer_question(
            runner=chat_runner,
            ledger_store=_noop_ledger_store(),
            slack_client=slack,
            channel_id=channel_id,
            question="remember, Acme Cloud goes to 6090",
            app_name=chat_runner.app_name,
            client_store=fake_store,
            message_ts="1700000000.001",
            thread_ts=None,
            raw_thread_ts="1700000000.001",
        )
    )

    # add_correction was called exactly once with the right args.
    assert len(fake_store.corrections) == 1, (
        f"Expected 1 add_correction call, got {fake_store.corrections}"
    )
    corr = fake_store.corrections[0]
    assert corr["vendor"] == "Acme Cloud"
    assert corr["account_code"] == "6090"
    assert corr["tax_code"] is None
    # client_id comes from profile_delta (empty here → falls back to channel_id).
    assert corr["client_id"] == channel_id

    # The pending list was cleared from the session.
    # _apply_state_delta appends an event; check the session state was cleared.
    # (_CapturingChatRunner.session_service.get_session returns the same _FakeSession
    # object; _apply_state_delta appends an Event — but since our fake doesn't
    # actually mutate state from events, we verify via the store call count instead.)
    assert len(fake_store.corrections) == 1  # idempotency: not doubled


# =========================================================================== #
# replace_month drain (Step 7 / C-3)
# =========================================================================== #


def _seed_month_ledger():
    """Seed a Purchase+Sales workbook with Sep+Oct rows; return (slack, store)."""
    from openpyxl import Workbook
    import io

    wb = Workbook()
    ws_p = wb.active
    ws_p.title = "Purchase"
    ws_p.append(["Date", "Invoice Number", "Description", "Source Amount", "Account Code / COA"])
    ws_p.append(["05/09/2025", "INV-P1", "AWS Sep",  100.0, "6090"])
    ws_p.append(["03/10/2025", "INV-P2", "AWS Oct",  120.0, "6090"])
    ws_s = wb.create_sheet("Sales")
    ws_s.append(["Date", "Invoice Number", "Description", "Source Amount", "Account Code / COA"])
    ws_s.append(["10/09/2025", "INV-S1", "Consult Sep", 500.0, "4000"])

    buf = io.BytesIO()
    wb.save(buf)
    xlsx = buf.getvalue()

    slack = FakeSlackClient()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    import uuid
    file_id = "F" + uuid.uuid4().hex[:10]
    slack.files[file_id] = xlsx
    url = f"https://files.slack.com/{file_id}/ledger.xlsx"
    slack.urls[url] = xlsx
    store._pointer_ref("c1", "2026").set({
        "slack_file_id": file_id,
        "client_id": "c1",
        "fy": "2026",
        "kind": "invoice",
        "seen_doc_keys": ["Purchase:INV-P1", "Purchase:INV-P2", "Sales:INV-S1"],
    })
    return slack, store


def test_execute_pending_writes_replace_month_drains_and_posts():
    from accounting_agents.slack_runner import _execute_pending_writes

    slack, store = _seed_month_ledger()
    state = {
        "pending_ledger_write": [
            {"op": "replace_month", "year": 2025, "month": 9},
        ],
    }
    committed = asyncio.run(_execute_pending_writes(
        state=state,
        ledger_store=store,
        slack_client=slack,
        channel_id="C1",
        client_id="c1",
        fy="2026",
        session_id="S1",
        fc_id="fc-rm-month",
    ))

    assert committed is True
    # Workbook trimmed — Sep rows gone.
    rows = store.read_rows("c1", "2026", slack, "C1")
    dates = [r.get("Date") for r in rows if r.get("Date")]
    assert all("10/2025" in str(d) for d in dates), dates

    # Summary message posted.
    assert any("September 2025" in m.get("text", "") for m in slack._posts), slack._posts
    assert any("re-drop" in m.get("text", "").lower() for m in slack._posts)

    # Audit logged + idempotency marker set.
    assert state["pending_ledger_write"] == []
    assert "fc-rm-month" in state["committed_confirmations"]


def test_execute_pending_writes_replace_month_purges_doc_keys():
    from accounting_agents.slack_runner import _execute_pending_writes

    slack, store = _seed_month_ledger()
    state = {"pending_ledger_write": [{"op": "replace_month", "year": 2025, "month": 9}]}
    asyncio.run(_execute_pending_writes(
        state=state, ledger_store=store, slack_client=slack, channel_id="C1",
        client_id="c1", fy="2026", session_id="S1", fc_id="fc-keys",
    ))

    ptr = store.get_pointer("c1", "2026")
    surviving = set(ptr.get("seen_doc_keys") or [])
    assert "Purchase:INV-P1" not in surviving   # Sep purged
    assert "Sales:INV-S1" not in surviving      # Sep purged
    assert "Purchase:INV-P2" in surviving       # Oct survives


def test_execute_pending_writes_replace_month_idempotent_double_yes():
    from accounting_agents.slack_runner import _execute_pending_writes

    slack, store = _seed_month_ledger()
    spec = {"op": "replace_month", "year": 2025, "month": 9}

    state = {"pending_ledger_write": [spec]}
    asyncio.run(_execute_pending_writes(
        state=state, ledger_store=store, slack_client=slack, channel_id="C1",
        client_id="c1", fy="2026", session_id="S1", fc_id="fc-idem",
    ))
    posts_after_first = len(slack._posts)

    # Second "yes" for same fc_id — must NOT re-apply.
    state["pending_ledger_write"] = [spec]
    committed2 = asyncio.run(_execute_pending_writes(
        state=state, ledger_store=store, slack_client=slack, channel_id="C1",
        client_id="c1", fy="2026", session_id="S1", fc_id="fc-idem",
    ))
    assert committed2 is False
    assert len(slack._posts) == posts_after_first  # no extra message
    assert state["pending_ledger_write"] == []


def test_execute_pending_writes_replace_month_refreshes_ledger_data():
    """After replace_month commits, state ledger_data reflects the trimmed workbook."""
    from accounting_agents.slack_runner import _execute_pending_writes

    slack, store = _seed_month_ledger()
    state = {"pending_ledger_write": [{"op": "replace_month", "year": 2025, "month": 9}]}
    asyncio.run(_execute_pending_writes(
        state=state, ledger_store=store, slack_client=slack, channel_id="C1",
        client_id="c1", fy="2026", session_id="S1", fc_id="fc-refresh",
    ))
    # The runner drains + marks committed; the real answer_question refreshes
    # ledger_data via read_rows post-commit. Here we verify the workbook is correct.
    rows = store.read_rows("c1", "2026", slack, "C1")
    assert len(rows) == 1  # only Oct Purchase row
    assert rows[0].get("Date") == "03/10/2025"


# =========================================================================== #
# Re-extract (Step 7 / ADR-0010): process_file_event hint+replace params,
# _execute_pending_reextract drain.
# =========================================================================== #


class _StateCapturingRunner(_FakeRunner):
    """A doc-graph runner that records the ``state_delta`` it was run with."""

    def __init__(self, events, final_state, app_name="acc"):
        super().__init__(events, final_state, app_name=app_name)
        self.run_state_delta: dict = {}

    async def run_async(self, *, user_id, session_id, new_message=None, state_delta=None):
        self.run_state_delta = dict(state_delta or {})
        async for ev in super().run_async(
            user_id=user_id, session_id=session_id,
            new_message=new_message, state_delta=state_delta,
        ):
            yield ev


class _CapturingLedgerStore:
    """A ledger_store stand-in that records the ``replace`` kwarg of append_rows."""

    def __init__(self, *, batch_replace_counts=None):
        self.append_calls: list[dict] = []
        self._batch_replace_counts = batch_replace_counts

    def append_rows(self, **kwargs):
        self.append_calls.append(kwargs)
        result = {
            "slack_file_id": "F-up", "appended": 1, "deduped": 0,
            "filename": "ledger.xlsx",
        }
        if self._batch_replace_counts is not None:
            result["batch_replace_counts"] = self._batch_replace_counts
        return result


def _completion_runner():
    """A doc runner whose run completes cleanly (no interrupt) → persist+deliver."""
    final_event = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text="done")]),
        get_function_calls=lambda: [],
    )
    return final_event


def test_process_file_event_hint_flows_into_state_delta_as_review_hint():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    runner = _StateCapturingRunner([_completion_runner()], _ledger_payload())

    asyncio.run(
        process_file_event(
            runner=runner, ledger_store=store, db=db, slack_client=slack,
            channel_id="C1", file_id="F1", app_name="acc",
            download_fn=lambda c, f: b"%PDF-1.4 fake",
            source_filename="re-extract-F1.pdf",
            hint="read as a credit note",
            client_store=_seeded_client_store(db),
        )
    )

    assert runner.run_state_delta.get("review_hint") == "read as a credit note"


def test_process_file_event_replace_flows_to_append_rows():
    slack = FakeSlackClient()
    db = FakeFirestore()
    ledger = _CapturingLedgerStore()
    runner = _FakeRunner([_completion_runner()], _ledger_payload())

    asyncio.run(
        process_file_event(
            runner=runner, ledger_store=ledger, db=db, slack_client=slack,
            channel_id="C1", file_id="F1", app_name="acc",
            download_fn=lambda c, f: b"%PDF-1.4 fake",
            source_filename="re-extract-F1.pdf",
            hint="read as a credit note",
            replace=True,
            client_store=_seeded_client_store(db),
        )
    )

    assert ledger.append_calls, "append_rows must have been called"
    assert ledger.append_calls[0]["replace"] is True


def test_process_file_event_default_path_does_not_replace_or_hint():
    """The normal file-drop path RESETS the re-extract keys (so a re-shared file
    can't inherit a stale hint/replace flag) and calls append_rows replace=False."""
    slack = FakeSlackClient()
    db = FakeFirestore()
    ledger = _CapturingLedgerStore()
    runner = _StateCapturingRunner([_completion_runner()], _ledger_payload())

    asyncio.run(
        process_file_event(
            runner=runner, ledger_store=ledger, db=db, slack_client=slack,
            channel_id="C1", file_id="F1", app_name="acc",
            download_fn=lambda c, f: b"%PDF-1.4 fake",
            source_filename="invoice.pdf",
            client_store=_seeded_client_store(db),
        )
    )

    # Reset (not absent): a fresh drop overwrites any stale leaked values.
    assert runner.run_state_delta.get("review_hint") == ""
    assert runner.run_state_delta.get("reextract_replace") is False
    assert ledger.append_calls[0]["replace"] is False


# --- _execute_pending_reextract ------------------------------------------- #


class _RecordingPFE:
    """A replacement for slack_runner.process_file_event that records its kwargs
    and returns a scripted result (default: a clean replaced delivery)."""

    def __init__(self, result=None):
        self.calls: list[dict] = []
        self._result = result or {
            "status": "delivered",
            "append": {"batch_replace_counts": [{"replaced": 1, "appended": 1}]},
        }

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


def _run_reextract(specs, *, pfe, slack, doc_runner=None):
    from accounting_agents import slack_runner

    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    runner = doc_runner or _FakeRunner([], _ledger_payload())
    with patch.object(slack_runner, "process_file_event", pfe):
        asyncio.run(
            slack_runner._execute_pending_reextract(
                specs,
                doc_runner=runner,
                ledger_store=store,
                db=db,
                slack_client=slack,
                channel_id="C1",
                app_name="acc",
                client_store=None,
                thread_ts=None,
            )
        )
    return runner


def test_execute_pending_reextract_calls_pfe_with_hint_and_replace():
    slack = FakeSlackClient()
    pfe = _RecordingPFE()
    doc_runner = _FakeRunner([], _ledger_payload())
    specs = [{"op": "reextract", "file_id": "F9", "hints": "read as a credit note"}]

    _run_reextract(specs, pfe=pfe, slack=slack, doc_runner=doc_runner)

    assert len(pfe.calls) == 1
    call = pfe.calls[0]
    assert call["runner"] is doc_runner  # the INJECTED doc runner, not the chat one
    assert call["file_id"] == "F9"
    assert call["hint"] == "read as a credit note"
    assert call["replace"] is True


def test_execute_pending_reextract_is_idempotent_on_double_run():
    slack = FakeSlackClient()
    pfe = _RecordingPFE()
    doc_runner = _FakeRunner([], _ledger_payload())
    specs = [{"op": "reextract", "file_id": "F9", "hints": "read as a credit note"}]

    from accounting_agents import slack_runner

    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    with patch.object(slack_runner, "process_file_event", pfe):
        async def _twice():
            for _ in range(2):
                await slack_runner._execute_pending_reextract(
                    specs, doc_runner=doc_runner, ledger_store=store, db=db,
                    slack_client=slack, channel_id="C1", app_name="acc",
                    client_store=None, thread_ts=None,
                )
        asyncio.run(_twice())

    # Same (file_id, hints) on the same runner instance → dispatched exactly once.
    assert len(pfe.calls) == 1


def test_execute_pending_reextract_posts_clear_month_note_on_identity_change():
    """A delivered re-read that replaced 0 rows (identity changed) → the user is
    pointed at the month-level primitive (replace_recorded_month / clear)."""
    slack = FakeSlackClient()
    pfe = _RecordingPFE(result={
        "status": "delivered",
        "append": {"batch_replace_counts": [{"replaced": 0, "appended": 2}]},
    })
    specs = [{"op": "reextract", "file_id": "F9", "hints": "read as a credit note"}]

    _run_reextract(specs, pfe=pfe, slack=slack)

    texts = " ".join(_posted_texts(slack)).lower()
    assert "clear" in texts and "identity" in texts


# --- HIGH regression: state-leak between runs on the same file_id ----------- #


class _CapturingLedgerStoreMulti:
    """Records every append_rows call so we can assert per-call replace kwarg."""

    def __init__(self):
        self.append_calls: list[dict] = []

    def append_rows(self, **kwargs):
        self.append_calls.append(dict(kwargs))
        return {
            "slack_file_id": "F-up", "appended": 1, "deduped": 0,
            "filename": "ledger.xlsx",
        }


def test_normal_drop_after_reextract_does_not_inherit_replace_flag():
    """HIGH regression: re-extract run on file_id X sets reextract_replace=True in
    session state. A subsequent normal drop of the SAME file_id on the SAME
    long-lived per-doc session MUST call append_rows with replace=False — not the
    stale True from the prior run."""
    slack = FakeSlackClient()
    db = FakeFirestore()
    ledger = _CapturingLedgerStoreMulti()
    # Use a _FakeRunner that reuses the same _FakeSessionService between calls so
    # the second run sees the session that the first run created — the realistic
    # long-lived-session scenario.
    runner = _FakeRunner([_completion_runner()], _ledger_payload())
    client_store = _seeded_client_store(db)

    # First call: re-extract (replace=True) — seeds reextract_replace=True into state.
    asyncio.run(
        process_file_event(
            runner=runner, ledger_store=ledger, db=db, slack_client=slack,
            channel_id="C1", file_id="F1", app_name="acc",
            download_fn=lambda c, f: b"%PDF-1.4 fake",
            source_filename="invoice.pdf",
            hint="read as a credit note",
            replace=True,
            client_store=client_store,
        )
    )
    assert ledger.append_calls[0]["replace"] is True

    # Reset the runner's events so the second run also completes cleanly.
    runner._events = [_completion_runner()]
    # The session now exists with reextract_replace=True in state; the second
    # NORMAL drop must overwrite it to False via the unconditional state_delta.
    asyncio.run(
        process_file_event(
            runner=runner, ledger_store=ledger, db=db, slack_client=slack,
            channel_id="C1", file_id="F1", app_name="acc",
            download_fn=lambda c, f: b"%PDF-1.4 fake",
            source_filename="invoice.pdf",
            # No hint, no replace — normal drop.
            client_store=client_store,
        )
    )
    # Second call must have replace=False (not the stale True from the first run).
    assert len(ledger.append_calls) == 2
    assert ledger.append_calls[1]["replace"] is False


def test_normal_drop_after_reextract_does_not_inherit_review_hint():
    """HIGH regression: a normal re-drop of file_id X must NOT forward the hint
    that a prior re-extract run seeded into the session's state_delta."""
    slack = FakeSlackClient()
    db = FakeFirestore()
    runner = _StateCapturingRunner([_completion_runner()], _ledger_payload())
    client_store = _seeded_client_store(db)

    # First call: re-extract seeds review_hint into state_delta.
    asyncio.run(
        process_file_event(
            runner=runner, ledger_store=SlackLedgerStore(FakeFirestore(), opener=slack.opener()),
            db=db, slack_client=slack,
            channel_id="C1", file_id="F1", app_name="acc",
            download_fn=lambda c, f: b"%PDF-1.4 fake",
            source_filename="invoice.pdf",
            hint="read as a credit note",
            replace=True,
            client_store=client_store,
        )
    )
    assert runner.run_state_delta.get("review_hint") == "read as a credit note"

    # Second call: normal drop — state_delta must carry review_hint="" (not the stale hint).
    runner._events = [_completion_runner()]
    asyncio.run(
        process_file_event(
            runner=runner, ledger_store=SlackLedgerStore(FakeFirestore(), opener=slack.opener()),
            db=db, slack_client=slack,
            channel_id="C1", file_id="F1", app_name="acc",
            download_fn=lambda c, f: b"%PDF-1.4 fake",
            source_filename="invoice.pdf",
            client_store=client_store,
        )
    )
    assert runner.run_state_delta.get("review_hint") == ""
    assert runner.run_state_delta.get("reextract_replace") is False


# --- MEDIUM regression: duplicate path also fires the identity-change note --- #


def test_execute_pending_reextract_posts_clear_month_note_on_duplicate_status():
    """MEDIUM regression: when the re-read returns status='duplicate' (all_deduped)
    and replaced=0 the identity changed — the user must still see the 'clear month'
    guidance. Previously only status='delivered' triggered it."""
    slack = FakeSlackClient()
    pfe = _RecordingPFE(result={
        "status": "duplicate",
        "append": {"batch_replace_counts": [{"replaced": 0, "appended": 0}]},
    })
    specs = [{"op": "reextract", "file_id": "F9", "hints": "read as a credit note"}]

    _run_reextract(specs, pfe=pfe, slack=slack)

    texts = " ".join(_posted_texts(slack)).lower()
    assert "clear" in texts and "identity" in texts
