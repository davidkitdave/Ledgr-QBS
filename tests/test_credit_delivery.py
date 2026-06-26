"""Hermetic tests for Slack-side credit gate and charge-on-delivery (D.2)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from accounting_agents.credit_delivery import (
    charge_delivery_credits,
    credit_gate_for_bytes,
    delivery_charge_units,
    delivery_idempotency_key,
    resolve_firm_id_from_state,
    wire_shared_credit_service,
)
from accounting_agents.ledger_store import SlackLedgerStore
from accounting_agents.slack_runner import process_file_event
from app.credit_service import CreditService, InMemoryCreditStore, configure_shared_credit_service
from tests._fake_firestore import FakeFirestore
from tests.test_slack_runner import FakeSlackClient


@pytest.fixture(autouse=True)
def credit_svc():
    store = InMemoryCreditStore()
    service = CreditService(store)
    configure_shared_credit_service(service)
    wire_shared_credit_service()
    yield service


def test_resolve_firm_id_prefers_explicit_firm_id() -> None:
    assert resolve_firm_id_from_state({"firm_id": "F1", "slack_team_id": "T1"}) == "F1"


def test_resolve_firm_id_falls_back_to_slack_team_id() -> None:
    assert resolve_firm_id_from_state({"slack_team_id": "T99"}) == "T99"


def test_delivery_charge_units_invoice_uses_appended_rows() -> None:
    # No page count → charge follows captured-doc count (floor of appended).
    units = delivery_charge_units(
        kind="invoice",
        payload={},
        append_result={"appended": 2, "deduped": 0},
    )
    assert units == 2


def test_charge_is_max_pages_or_captured_docs() -> None:
    # A 3-page single invoice → 3 credits (by pages).
    assert (
        delivery_charge_units(
            kind="invoice",
            payload={"input_page_count": 3},
            append_result={"appended": 1, "deduped": 0},
            input_page_count=3,
        )
        == 3
    )
    # Page count via payload only (no explicit param) still charges by pages.
    assert (
        delivery_charge_units(
            kind="invoice",
            payload={"input_page_count": 3},
            append_result={"appended": 1, "deduped": 0},
        )
        == 3
    )
    # A single page holding 3 receipts → 3 credits (by captured docs, NOT 1).
    assert (
        delivery_charge_units(
            kind="receipt",
            payload={"input_page_count": 1},
            append_result={"appended": 3, "deduped": 0},
            input_page_count=1,
        )
        == 3
    )
    # A 1-page single invoice → 1 credit.
    assert (
        delivery_charge_units(
            kind="invoice",
            payload={"input_page_count": 1},
            append_result={"appended": 1, "deduped": 0},
            input_page_count=1,
        )
        == 1
    )
    # Unknown page count falls back to captured-doc count (never below appended).
    assert (
        delivery_charge_units(
            kind="invoice",
            payload={},
            append_result={"appended": 2, "deduped": 0},
        )
        == 2
    )
    # Fully deduped re-submit → 0 (dedup yields no charge).
    assert (
        delivery_charge_units(
            kind="invoice",
            payload={"input_page_count": 3},
            append_result={"appended": 0, "all_deduped": True},
            input_page_count=3,
        )
        == 0
    )


def test_delivery_charge_units_bank_uses_page_count() -> None:
    units = delivery_charge_units(
        kind="bank",
        payload={"input_page_count": 3},
        append_result={"appended": 1, "deduped": 0},
        input_page_count=3,
    )
    assert units == 3


def test_delivery_charge_units_skips_deduped() -> None:
    assert (
        delivery_charge_units(
            kind="invoice",
            payload={},
            append_result={"appended": 0, "all_deduped": True},
        )
        == 0
    )


def test_charge_delivery_credits_is_idempotent(credit_svc: CreditService) -> None:
    credit_svc.ensure_firm("T1")
    credit_svc.grant("T1", 5, note="trial")
    append = {"appended": 1, "deduped": 0}
    payload = {"kind": "invoice", "file_id": "F1"}
    first = charge_delivery_credits(
        firm_id="T1",
        channel_id="C1",
        file_id="F1",
        kind="invoice",
        payload=payload,
        append_result=append,
    )
    second = charge_delivery_credits(
        firm_id="T1",
        channel_id="C1",
        file_id="F1",
        kind="invoice",
        payload=payload,
        append_result=append,
    )
    assert first == {"credits_used": 1, "credits_remaining": 4}
    assert second == {"credits_used": 1, "credits_remaining": 4}
    assert credit_svc.read_balance("T1") == 4


def test_credit_gate_blocks_zero_balance(credit_svc: CreditService) -> None:
    credit_svc.ensure_firm("T0")
    decision = credit_gate_for_bytes(
        firm_id="T0",
        data=b"%PDF fake",
        filename="invoice.pdf",
    )
    assert decision["allowed"] is False
    assert decision["reason"] == "zero_credit"


def _seeded_store_with_firm(db: FakeFirestore, firm_id: str = "TQA"):
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


def test_process_file_event_blocks_before_clean_agent_when_zero_credit(monkeypatch) -> None:
    monkeypatch.setenv("LEDGR_USE_CLEAN_AGENT", "1")
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    class _Runner:
        artifact_service = None
        session_service = None

    with patch("ledgr_agent.tools.process_document_batch") as mock_tool:
        result = asyncio.run(
            process_file_event(
                runner=_Runner(),
                ledger_store=store,
                db=db,
                slack_client=slack,
                channel_id="C1",
                file_id="F-zero",
                app_name="acc",
                download_fn=lambda c, f: b"%PDF-1.4 fake",
                source_filename="zero.pdf",
                client_store=_seeded_store_with_firm(db, "T0"),
            )
        )
        mock_tool.assert_not_called()

    assert result["status"] == "blocked"
    assert result["reason"] == "zero_credit"
    assert slack.uploads == []


def test_process_file_event_clean_agent_charges_on_delivery(
    monkeypatch, credit_svc: CreditService
) -> None:
    monkeypatch.setenv("LEDGR_USE_CLEAN_AGENT", "1")
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

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

    class _Runner:
        def __init__(self) -> None:
            self.artifact_service = None
            self.session_service = _StatefulSessionService()

        async def run_async(self, **kwargs):
            if False:
                yield None

    credit_svc.ensure_firm("TQA")
    credit_svc.grant("TQA", 10, note="trial")

    success_batch = {
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
        "credits": {"credit_status": "estimated"},
    }

    with patch("ledgr_agent.tools.process_document_batch", return_value=success_batch):
        result = asyncio.run(
            process_file_event(
                runner=_Runner(),
                ledger_store=store,
                db=db,
                slack_client=slack,
                channel_id="C1",
                file_id="F-charge",
                app_name="acc",
                download_fn=lambda c, f: b"%PDF-1.4 fake",
                source_filename="clean.pdf",
                client_store=_seeded_store_with_firm(db, "TQA"),
            )
        )

    assert result["status"] == "delivered"
    assert credit_svc.read_balance("TQA") == 9
    footer_posts = [
        msg
        for msg in slack._posts
        if any(
            "Used 1 credit" in str(block)
            for block in (msg.get("blocks") or [])
        )
        or "Used 1 credit" in str(msg.get("text") or "")
    ]
    assert footer_posts, "expected credit footer on delivery card"


def test_delivery_idempotency_key_format() -> None:
    assert delivery_idempotency_key(channel_id="C1", file_id="F1") == "C1:F1:deliver"


def test_flush_deferred_ledger_writes_charges_credits_per_doc(credit_svc: CreditService) -> None:
    """Batch-deferred Slack drops must charge after the batch-end flush (ADR-0016)."""
    from accounting_agents.slack_runner import _flush_deferred_ledger_writes
    from invoice_processing.export.client_context import FirestoreClientStore

    db = FakeFirestore()
    profile = {
        "client_id": "c1",
        "client_name": "Test Client",
        "fye_month": 12,
        "accounting_software": "QBS Ledger",
        "gst_registered": True,
        "region": "SINGAPORE",
        "base_currency": "SGD",
        "status": "active",
        "firm_id": "TQA",
        "slack_team_id": "TQA",
    }
    db.collection("clients").document("c1").set(profile)
    db.collection("channels").document("C-batch-charge").set({"client_id": "c1"})
    client_store = FirestoreClientStore(client=db)

    slack = FakeSlackClient()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    credit_svc.ensure_firm("TQA")
    credit_svc.grant("TQA", 10, note="trial")

    batch_deferred = [
        {
            "file_id": "F-batch-1",
            "effective_replace": False,
            "payload": {
                "client_id": "c1",
                "fy": "2026",
                "kind": "invoice",
                "software": "qbs",
                "client_name": "Test Client",
                "input_page_count": 1,
            },
            "batches": [
                {
                    "sheet": "Purchase",
                    "doc_key": "F-batch-1:Purchase:INV-1",
                    "rows": [{"Invoice Number": "INV-1", "Description": "a", "Source Amount": 10.0}],
                }
            ],
            "workbook_name": "",
        },
        {
            "file_id": "F-batch-2",
            "effective_replace": False,
            "payload": {
                "client_id": "c1",
                "fy": "2026",
                "kind": "invoice",
                "software": "qbs",
                "client_name": "Test Client",
                "input_page_count": 1,
            },
            "batches": [
                {
                    "sheet": "Purchase",
                    "doc_key": "F-batch-2:Purchase:INV-2",
                    "rows": [{"Invoice Number": "INV-2", "Description": "b", "Source Amount": 20.0}],
                }
            ],
            "workbook_name": "",
        },
    ]

    asyncio.run(
        _flush_deferred_ledger_writes(
            ledger_store=store,
            slack_client=slack,
            channel_id="C-batch-charge",
            batch_deferred=batch_deferred,
            client_store=client_store,
        )
    )

    assert credit_svc.read_balance("TQA") == 8, "two delivered docs should charge 2 credits total"

    # Re-flush the same deferred payloads: idempotency keys must prevent double-charge.
    asyncio.run(
        _flush_deferred_ledger_writes(
            ledger_store=store,
            slack_client=slack,
            channel_id="C-batch-charge",
            batch_deferred=batch_deferred,
            client_store=client_store,
        )
    )
    assert credit_svc.read_balance("TQA") == 8, "re-flush must not double-charge"
