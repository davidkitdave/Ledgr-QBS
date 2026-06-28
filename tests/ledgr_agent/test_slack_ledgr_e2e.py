"""E2E: Slack file upload → ledgr_agent tools → FY workbook delivery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ledgr_agent.billing as billing
from accounting_agents.credit_delivery import wire_shared_credit_service
from accounting_agents.ledger_store import SlackLedgerStore
from accounting_agents.slack_runner import process_file_event
from app.credit_service import CreditService, InMemoryCreditStore, configure_shared_credit_service
from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY
from tests._fake_firestore import FakeFirestore
from tests.test_slack_runner import FakeSlackClient, _seeded_client_store
from app.native_blocks_compat import _reset_for_tests


@pytest.fixture(autouse=True)
def _credit_setup() -> None:
    billing._shared_credit_service = None
    wire_shared_credit_service()
    service = CreditService(InMemoryCreditStore())
    service.ensure_firm("T_TEST")
    service.grant("T_TEST", 10, note="test")
    configure_shared_credit_service(service)
    yield
    billing._shared_credit_service = None


@pytest.fixture(autouse=True)
def _force_fallback_blocks(monkeypatch):
    monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "0")
    _reset_for_tests()
    yield
    _reset_for_tests()


class _StatefulSessionService:
    def __init__(self) -> None:
        self.state: dict = {}
        self.created = False

    async def get_session(self, *, app_name, user_id, session_id):
        if not self.created:
            return None
        return SimpleNamespace(state=dict(self.state))

    async def create_session(self, *, app_name, user_id, session_id, state=None):
        self.created = True
        self.state = dict(state or {})
        return SimpleNamespace(state=dict(self.state))

    async def append_event(self, session, event):
        delta = getattr(getattr(event, "actions", None), "state_delta", None) or {}
        self.state.update(delta)


class _LedgrRunner:
    def __init__(self) -> None:
        self.artifact_service = SimpleNamespace(save_artifact=lambda *a, **k: None)
        self.session_service = _StatefulSessionService()


def _seeded_store_with_firm(db: FakeFirestore, firm_id: str = "T_TEST"):
    from invoice_processing.export.client_context import FirestoreClientStore

    profile = {
        "client_id": "c1",
        "client_name": "Test Client",
        "fye_month": 12,
        "accounting_software": "QBS Ledger",
        "gst_registered": True,
        "region": "SINGAPORE",
        "base_currency": "SGD",
        "status": "active",
        "firm_id": firm_id,
        "slack_team_id": firm_id,
    }
    db.collection("clients").document("c1").set(profile)
    db.collection("channels").document("C1").set({"client_id": "c1"})
    return FirestoreClientStore(client=db)


def _mock_read_payload() -> dict:
    return {
        "file_kind": "commercial_documents",
        "source_path": "/tmp/invoice.pdf",
        "page_count": 1,
        "document_count": 1,
        "credit_units": 1,
        "documents": [
            {
                "doc_type": "purchase",
                "vendor_name": "Acme Pte Ltd",
                "invoice_number": "INV-9001",
                "invoice_date": "2026-01-10",
                "currency": "SGD",
                "lines": [
                    {
                        "description": "Widget",
                        "net_amount": 100.0,
                        "tax_amount": 9.0,
                        "total_amount": 109.0,
                    }
                ],
            }
        ],
    }


def test_process_file_event_ledgr_agent_delivers(monkeypatch) -> None:
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    runner = _LedgrRunner()

    def _fake_read(tool_context, paths=None):
        tool_context.state[READ_DOC_STATE_KEY] = _mock_read_payload()
        return {"status": "success", "file_kind": "commercial_documents"}

    with patch("ledgr_agent.runtime.slack_shell.read_doc", side_effect=_fake_read):
        result = asyncio.run(
            process_file_event(
                runner=runner,
                ledger_store=store,
                db=db,
                slack_client=slack,
                channel_id="C1",
                file_id="F-ledgr-v1",
                app_name="ledgr_agent",
                download_fn=lambda c, f: b"%PDF-1.4 fake",
                source_filename="invoice.pdf",
                client_store=_seeded_store_with_firm(db),
                thread_ts="1716000000.000200",
            )
        )

    assert result["status"] == "delivered"
    assert len(slack.uploads) == 1
    assert slack._posts, "expected delivery summary message"


def test_process_file_event_blocked_at_zero_credit(monkeypatch) -> None:
    billing.configure_shared_credit_service(CreditService(InMemoryCreditStore()))

    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    runner = _LedgrRunner()

    result = asyncio.run(
        process_file_event(
            runner=runner,
            ledger_store=store,
            db=db,
            slack_client=slack,
            channel_id="C1",
            file_id="F-blocked",
            app_name="ledgr_agent",
            download_fn=lambda c, f: b"%PDF-1.4 fake",
            source_filename="invoice.pdf",
            client_store=_seeded_store_with_firm(db, firm_id="T_ZERO"),
        )
    )

    assert result["status"] == "blocked"
    assert not slack.uploads
