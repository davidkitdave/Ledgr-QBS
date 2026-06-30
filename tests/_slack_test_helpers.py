"""Shared Slack test fakes for ledgr_agent + slack_runner hermetic tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ledgr_slack.app import build_async_app
from tests._fake_firestore import FakeFirestore
from tests.test_ledger_store import FakeSlackClient

LEDGER_ROWS_KEY = "ledger_rows"
DELIVER_SUMMARY_KEY = "deliver_summary"

_TEST_PROFILE: dict = {
    "client_id": "c1",
    "client_name": "Test Client",
    "fye_month": 12,
    "accounting_software": "QBS Ledger",
    "gst_registered": True,
    "region": "SINGAPORE",
    "base_currency": "SGD",
    "status": "active",
    "firm_id": "T_TEST",
    "slack_team_id": "T_TEST",
}

_SOFTWARE_DISPLAY = {
    "qbs": "QBS Ledger",
    "xero": "Xero",
    "autocount": "AutoCount",
    "sql_account": "SQL Account",
}


class _FakeArtifactService:
    def __init__(self) -> None:
        self.saved: dict = {}

    async def save_artifact(
        self, *, app_name, user_id, filename, artifact, session_id=None, custom_metadata=None
    ):
        self.saved[(user_id, filename)] = artifact
        return 0


class _FakeSession:
    def __init__(self, state: dict) -> None:
        self.state = dict(state)


class _FakeSessionService:
    def __init__(self, final_state: dict | None = None) -> None:
        self._final_state = dict(final_state or {})
        self.created = False

    async def get_session(self, *, app_name, user_id, session_id):
        if not self.created:
            return None
        return _FakeSession(self._final_state)

    async def create_session(self, *, app_name, user_id, session_id, state=None):
        self.created = True
        if state:
            self._final_state.update(state)
        return _FakeSession(self._final_state)

    async def append_event(self, session, event):
        delta = getattr(getattr(event, "actions", None), "state_delta", None) or {}
        self._final_state.update(delta)


class _FakeRunner:
    """Minimal runner stand-in for process_file_event tests."""

    def __init__(self, events, final_state, app_name: str = "acc") -> None:
        self.app_name = app_name
        self.artifact_service = _FakeArtifactService()
        self.session_service = _FakeSessionService(final_state)
        self._events = events

    async def run_async(self, *, user_id, session_id, new_message=None, state_delta=None):
        self.session_service.created = True
        for ev in self._events:
            yield ev


def ledger_payload(sheet: str = "Purchase", doc_key: str = "F1:Purchase:INV-1") -> dict:
    return {
        LEDGER_ROWS_KEY: {
            "client_id": "c1",
            "fy": "2026",
            "kind": "invoice",
            "software": "qbs",
            "file_id": "F1",
            "batches": [
                {
                    "sheet": sheet,
                    "doc_key": doc_key,
                    "rows": [{"Invoice Number": "INV-1", "Description": "x", "Source Amount": 10.0}],
                }
            ],
        },
        DELIVER_SUMMARY_KEY: "📒 Added 1 line from 1 document to your FY2026 ledger.",
    }


def seeded_client_store(
    db: FakeFirestore,
    channel_id: str = "C1",
    client_id: str = "c1",
    software: str | None = None,
):
    from ledgr_slack.client_context import FirestoreClientStore

    profile = dict(_TEST_PROFILE, client_id=client_id)
    if software:
        profile["accounting_software"] = _SOFTWARE_DISPLAY.get(software, software)
    db.collection("clients").document(client_id).set(profile)
    db.collection("channels").document(channel_id).set({"client_id": client_id})
    return FirestoreClientStore(client=db)


def posted_texts(slack: FakeSlackClient) -> list[str]:
    return [p.get("text", "") for p in getattr(slack, "_posts", [])]


def capture_message_handler_with_slack_client(
    injected_slack: FakeSlackClient,
    *,
    ledger_store=None,
):
    from app.slack_app import _SeenEvents

    registered: dict = {}
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
    db = FakeFirestore()
    from ledgr_slack.client_context import FirestoreClientStore

    store = FirestoreClientStore(client=db)

    with (
        patch("ledgr_slack.dedup._seen", fresh_seen),
        patch("slack_bolt.async_app.AsyncApp", return_value=fake_app),
        patch("slack_sdk.WebClient", return_value=injected_slack),
    ):
        build_async_app(
            runner=rm,
            ledger_store=ledger_store if ledger_store is not None else MagicMock(),
            db=db,
            store=store,
        )

    return registered["message"], fresh_seen


@pytest.fixture
def mock_ledgr_read_doc(monkeypatch):
    """Hermetic read_doc for process_file_event tests (ledgr_agent path)."""
    import ledgr_agent.billing as billing
    from ledgr_agent.billing import CreditService, InMemoryCreditStore, configure_shared_credit_service

    billing._shared_credit_service = None
    service = CreditService(InMemoryCreditStore())
    service.ensure_firm("T_TEST")
    service.grant("T_TEST", 100, note="test")
    configure_shared_credit_service(service)

    def _fake_read(tool_context, paths=None):
        from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY

        tool_context.state[READ_DOC_STATE_KEY] = {
            "file_kind": "commercial_documents",
            "source_path": "/tmp/invoice.pdf",
            "page_count": 1,
            "document_count": 1,
            "credit_units": 1,
            "documents": [
                {
                    "doc_type": "purchase",
                    "invoice_number": "INV-1",
                    "invoice_date": "2026-06-24",
                    "lines": [{"description": "x", "net_amount": 10.0}],
                }
            ],
        }
        return {"status": "success", "file_kind": "commercial_documents"}

    monkeypatch.setattr("ledgr_slack.slack_shell.read_doc", _fake_read)
    yield
    billing._shared_credit_service = None
