"""Hermetic tests for Slack-side credit gate and charge-on-delivery (D.2)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from ledgr_slack.credit_adapter import (
    charge_delivery_credits,
    credit_gate_for_bytes,
    delivery_charge_units,
    delivery_idempotency_key,
    resolve_firm_id_from_state,
    wire_shared_credit_service,
)
from ledgr_slack.ledger_store import SlackLedgerStore
from ledgr_slack.app import process_file_event
from ledgr_agent.billing import CreditService, InMemoryCreditStore, configure_shared_credit_service
from tests._fake_firestore import FakeFirestore
from tests.test_ledger_store import FakeSlackClient


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


def test_process_file_event_blocks_before_ledgr_when_zero_credit(monkeypatch) -> None:
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    class _Runner:
        artifact_service = None
        session_service = None

    with patch("ledgr_slack.slack_shell.read_doc") as mock_read:
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
        mock_read.assert_not_called()

    assert result["status"] == "blocked"
    assert result["reason"] == "zero_credit"
    assert slack.uploads == []


def test_process_file_event_ledgr_charges_on_build_sheets(
    monkeypatch, credit_svc: CreditService
) -> None:
    from types import SimpleNamespace

    from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY

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
            self.artifact_service = SimpleNamespace(save_artifact=lambda *a, **k: None)
            self.session_service = _StatefulSessionService()

    credit_svc.ensure_firm("TQA")
    credit_svc.grant("TQA", 10, note="trial")

    def _fake_read(tool_context, paths=None):
        tool_context.state[READ_DOC_STATE_KEY] = {
            "file_kind": "commercial_documents",
            "source_path": "/tmp/clean.pdf",
            "page_count": 1,
            "document_count": 1,
            "credit_units": 1,
            "documents": [
                {
                    "doc_type": "purchase",
                    "invoice_number": "INV-9001",
                    "invoice_date": "2026-01-10",
                    "lines": [{"description": "Widget", "net_amount": 50.0}],
                }
            ],
        }
        return {"status": "success", "file_kind": "commercial_documents"}

    with patch("ledgr_slack.slack_shell.read_doc", side_effect=_fake_read):
        result = asyncio.run(
            process_file_event(
                runner=_Runner(),
                ledger_store=store,
                db=db,
                slack_client=slack,
                channel_id="C1",
                file_id="F-charge",
                app_name="ledgr_agent",
                download_fn=lambda c, f: b"%PDF-1.4 fake",
                source_filename="clean.pdf",
                client_store=_seeded_store_with_firm(db, "TQA"),
            )
        )

    assert result["status"] == "delivered"
    assert credit_svc.read_balance("TQA") == 9


def test_delivery_idempotency_key_format() -> None:
    assert delivery_idempotency_key(channel_id="C1", file_id="F1") == "C1:F1:deliver"
