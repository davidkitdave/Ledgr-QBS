"""Integration: fixture read_doc JSON → build_sheets → FY ledger xlsx (no live Gemini)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import ledgr_agent.billing as billing
from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY
from ledgr_slack.credit_adapter import wire_shared_credit_service
from ledgr_slack.file_event import process_file_event
from ledgr_slack.ledger_store import SlackLedgerStore
from ledgr_agent.billing import CreditService, InMemoryCreditStore, configure_shared_credit_service
from tests._fake_firestore import FakeFirestore
from tests.ledgr_agent.test_slack_ledgr_e2e import (
    _LedgrRunner,
    _mock_read_payload,
    _seeded_store_with_firm,
)
from tests.test_ledger_store import FakeSlackClient, _read_sheet_rows

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _credit_setup() -> None:
    billing._shared_credit_service = None
    wire_shared_credit_service()
    service = CreditService(InMemoryCreditStore())
    service.ensure_firm("T_TEST")
    service.grant("T_TEST", 10, note="integration test")
    configure_shared_credit_service(service)
    yield
    billing._shared_credit_service = None


def test_light_path_fixture_read_to_fy_workbook() -> None:
    """Hermetic spine: mocked read → build_sheets → append_rows → xlsx on Slack."""
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
                file_id="F-integration-1",
                app_name="ledgr_agent",
                download_fn=lambda c, f: b"%PDF-1.4 fake",
                source_filename="invoice.pdf",
                client_store=_seeded_store_with_firm(db),
                thread_ts="1716000000.000400",
            )
        )

    assert result["status"] == "delivered"
    assert len(slack.uploads) == 1
    upload = slack.uploads[0]
    assert upload["filename"] == "Test Client - Ledger_FY2026.xlsx"

    delivery = result.get("delivery") or {}
    payload = delivery.get("payload") or {}
    batches = payload.get("batches") or []
    assert batches, "expected ledger batches in delivery payload"
    assert batches[0]["rows"][0].get("Invoice Number") == "INV-9001"
    assert payload.get("fy") == "2026"

    rows = _read_sheet_rows(slack.files[upload["id"]], "Purchase")
    assert len(rows) == 1
    assert rows[0][0] is not None  # first column (Date) populated
