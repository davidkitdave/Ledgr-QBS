"""E2E: Slack file upload → ledgr_agent tools → FY workbook delivery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import ledgr_agent.billing as billing
from ledgr_slack.credit_adapter import wire_shared_credit_service
from ledgr_slack.ledger_store import SlackLedgerStore
from ledgr_slack.file_event import process_file_event
from ledgr_agent.billing import CreditService, InMemoryCreditStore, configure_shared_credit_service
from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY
from tests._fake_firestore import FakeFirestore
from tests.test_ledger_store import FakeSlackClient
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
    from ledgr_slack.client_context import FirestoreClientStore

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

    with patch("ledgr_slack.slack_shell.read_doc", side_effect=_fake_read):
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


def test_hierarchy_bill_delivers_summary_line_count_not_detail_rows(monkeypatch) -> None:
    """Slack row count mirrors read_doc lines[] — three bookable rows, not appendix noise."""
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    runner = _LedgrRunner()

    summary_lines = [
        {"description": "Internet Services", "net_amount": 169.42, "tax_amount": 0.0},
        {"description": "Mobile Services", "net_amount": 1041.93, "tax_amount": 0.0},
        {"description": "Switch Services", "net_amount": 12.0, "tax_amount": 0.0},
    ]

    def _fake_read(tool_context, paths=None):
        tool_context.state[READ_DOC_STATE_KEY] = {
            "file_kind": "commercial_documents",
            "source_path": "/tmp/telco.pdf",
            "page_count": 3,
            "document_count": 1,
            "credit_units": 1,
            "documents": [
                {
                    "doc_type": "purchase",
                    "document_kind": "invoice",
                    "vendor_name": "StarHub Ltd",
                    "invoice_number": "800448392",
                    "invoice_date": "2025-12-04",
                    "currency": "SGD",
                    "subtotal": 1223.35,
                    "tax_total": 104.80,
                    "grand_total": 1328.15,
                    "lines": summary_lines,
                }
            ],
        }
        return {"status": "success", "file_kind": "commercial_documents"}

    with patch("ledgr_slack.slack_shell.read_doc", side_effect=_fake_read):
        result = asyncio.run(
            process_file_event(
                runner=runner,
                ledger_store=store,
                db=db,
                slack_client=slack,
                channel_id="C1",
                file_id="F-telco-summary",
                app_name="ledgr_agent",
                download_fn=lambda c, f: b"%PDF-1.4 fake",
                source_filename="telco.pdf",
                client_store=_seeded_store_with_firm(db),
                thread_ts="1716000000.000201",
            )
        )

    assert result["status"] == "delivered"
    delivery = result.get("delivery") or {}
    summary = delivery.get("summary") or ""
    assert "3 line" in summary
    payload = delivery.get("payload") or {}
    batches = payload.get("batches") or []
    row_count = sum(len(b.get("rows") or []) for b in batches)
    assert row_count == 3
    for row in batches[0]["rows"]:
        assert row["Total Amount"] != 1328.15


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
