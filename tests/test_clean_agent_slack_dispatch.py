"""Hermetic tests for the clean-agent Slack dispatch path (D.3 + D.4)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from accounting_agents import nodes
from accounting_agents.clean_agent_slack import handle_clean_agent_approval_action
from accounting_agents.hitl import write_interrupt
from accounting_agents.ledger_store import SlackLedgerStore
from accounting_agents.slack_runner import process_file_event
from ledgr_agent.slack.hitl_bridge import CLEAN_AGENT_HITL_KIND
from tests._fake_firestore import FakeFirestore
from tests.test_slack_runner import FakeSlackClient, _seeded_client_store
from app.native_blocks_compat import _reset_for_tests


@pytest.fixture(autouse=True)
def _force_fallback_blocks(monkeypatch):
    monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "0")
    _reset_for_tests()
    yield
    _reset_for_tests()


def _success_batch() -> dict:
    return {
        "status": "success",
        "client_id": "c1",
        "posted_documents": [
            {
                "doc_type": "invoice",
                "invoice_number": "INV-9001",
                "sheet": "Purchase",
                "file_name": "clean.pdf",
            }
        ],
        "per_file": [{"doc_type": "invoice", "file_name": "clean.pdf"}],
        "export_rows": [
            {
                "workbook": "Ledger_FY2026.xlsx",
                "sheet": "Purchase",
                "Invoice Number": "INV-9001",
                "Description": "Widget",
                "Amount": 50.0,
            }
        ],
        "review_requests": [],
        "validation_summary": {},
        "credits": {"credit_status": "not_billable"},
    }


class _StatefulSessionService:
    def __init__(self) -> None:
        self.state: dict = {}
        self.created = False

    async def get_session(self, *, app_name, user_id, session_id):
        if not self.created:
            return None
        from types import SimpleNamespace
        return SimpleNamespace(state=dict(self.state))

    async def create_session(self, *, app_name, user_id, session_id, state=None):
        self.created = True
        self.state = dict(state or {})
        from types import SimpleNamespace
        return SimpleNamespace(state=dict(self.state))

    async def append_event(self, session, event):
        delta = getattr(getattr(event, "actions", None), "state_delta", None) or {}
        self.state.update(delta)


class _CleanAgentRunner:
    def __init__(self) -> None:
        self.artifact_service = SimpleNamespace(save_artifact=lambda *a, **k: None)
        self.session_service = _StatefulSessionService()

    async def run_async(self, **kwargs):
        if False:
            yield None
        return


def test_process_file_event_clean_agent_delivers_without_graph(monkeypatch) -> None:
    monkeypatch.setenv("LEDGR_USE_CLEAN_AGENT", "1")
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    runner = _CleanAgentRunner()

    with patch(
        "ledgr_agent.tools.process_document_batch",
        return_value=_success_batch(),
    ):
        result = asyncio.run(
            process_file_event(
                runner=runner,
                ledger_store=store,
                db=db,
                slack_client=slack,
                channel_id="C1",
                file_id="F-clean-d3",
                app_name="acc",
                download_fn=lambda c, f: b"%PDF-1.4 fake",
                source_filename="clean.pdf",
                client_store=_seeded_client_store(db),
                thread_ts="1716000000.000200",
            )
        )

    assert result["status"] == "delivered"
    assert len(slack.uploads) == 1
    assert slack._posts, "expected a delivery card message"


def test_process_file_event_clean_agent_blocked_skips_delivery(monkeypatch) -> None:
    monkeypatch.setenv("LEDGR_USE_CLEAN_AGENT", "1")
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    runner = _CleanAgentRunner()

    blocked = {
        "status": "blocked",
        "client_id": "c1",
        "validation_summary": {"block_reason": "zero_credit"},
        "credits": {"credits_remaining": 0, "credit_status": "blocked"},
        "export_rows": [],
        "posted_documents": [],
    }

    with patch(
        "ledgr_agent.tools.process_document_batch",
        return_value=blocked,
    ):
        result = asyncio.run(
            process_file_event(
                runner=runner,
                ledger_store=store,
                db=db,
                slack_client=slack,
                channel_id="C1",
                file_id="F-blocked",
                app_name="acc",
                download_fn=lambda c, f: b"%PDF fake",
                source_filename="blocked.pdf",
                client_store=_seeded_client_store(db),
            )
        )

    assert result["status"] == "blocked"
    assert slack.uploads == []
    assert any("credit" in (msg.get("text") or "").lower() for msg in slack._posts)


def _needs_review_batch() -> dict:
    return {
        "status": "needs_review",
        "client_id": "c1",
        "posted_documents": [],
        "skipped_documents": [{"doc_type": "invoice", "note": "needs review: low COA"}],
        "per_file": [{"doc_type": "invoice", "file_name": "review.pdf"}],
        "review_requests": [
            {"id": "low_coa", "severity": "review", "message": "2 lines have low-confidence account mapping."}
        ],
        "export_rows": [],
        "validation_summary": {},
    }


def test_process_file_event_clean_agent_pause_posts_approval_card(monkeypatch) -> None:
    monkeypatch.setenv("LEDGR_USE_CLEAN_AGENT", "1")
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    runner = _CleanAgentRunner()

    with patch(
        "ledgr_agent.tools.process_document_batch",
        return_value=_needs_review_batch(),
    ):
        result = asyncio.run(
            process_file_event(
                runner=runner,
                ledger_store=store,
                db=db,
                slack_client=slack,
                channel_id="C1",
                file_id="F-review",
                app_name="acc",
                download_fn=lambda c, f: b"%PDF fake",
                source_filename="review.pdf",
                client_store=_seeded_client_store(db),
            )
        )

    assert result["status"] == "paused"
    assert result["op_id"] == "C1:F-review"
    assert slack.uploads == []
    action_ids: set[str] = set()
    for post in slack._posts:
        for block in post.get("blocks") or []:
            if block.get("type") == "actions":
                action_ids.update(e["action_id"] for e in block.get("elements") or [])
    assert action_ids == {"approve", "edit", "reject"}
    snap = db.collection("interrupts").document("C1:F-review").get()
    assert snap.exists
    assert snap.to_dict()["kind"] == CLEAN_AGENT_HITL_KIND


def test_clean_agent_approve_delivers_stashed_ledger(monkeypatch) -> None:
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    runner = _CleanAgentRunner()

    payload = {
        "client_id": "c1",
        "client_name": "Test Client",
        "fy": "2026",
        "kind": "invoice",
        "software": "QBS Ledger",
        "file_id": "F-hitl",
        "batches": [
            {
                "sheet": "Purchase",
                "doc_key": "Purchase:INV-HITL",
                "rows": [{"Invoice Number": "INV-HITL", "Description": "x", "Amount": 12.0}],
            }
        ],
    }
    write_interrupt(
        db,
        "C1:F-hitl",
        session_id="C1:F-hitl",
        channel_id="C1",
        slack_file_id="F-hitl",
        message_ts="100.001",
        user_id="C1",
        extra={
            "kind": CLEAN_AGENT_HITL_KIND,
            "summary": "Please review",
            "ledger_payload": payload,
            "batch_result": {"status": "needs_review"},
        },
    )

    result = asyncio.run(
        handle_clean_agent_approval_action(
            runner=runner,
            ledger_store=store,
            db=db,
            slack_client=slack,
            op_id="C1:F-hitl",
            decision="approve",
            app_name="acc",
            client_store=_seeded_client_store(db),
        )
    )

    assert result["status"] == "resumed"
    assert len(slack.uploads) == 1
    session = asyncio.run(
        runner.session_service.get_session(
            app_name="acc", user_id="C1", session_id="C1:F-hitl"
        )
    )
    assert session is not None
    assert session.state.get(nodes.LEDGER_ROWS_KEY)
