"""Tests for the loud-alert / require-firm policy on the LIVE Slack gate.

When firm_id cannot be resolved for a real upload the code must log loudly
(billing anomaly) instead of silently skipping; with LEDGR_CREDIT_REQUIRE_FIRM
set it must block the upload before any LLM work.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from accounting_agents.credit_delivery import (
    flag_unresolved_firm_billing_anomaly,
    require_firm_for_billing,
)
from accounting_agents.ledger_store import SlackLedgerStore
from accounting_agents.slack_runner import process_file_event
from app.credit_service import CreditService, InMemoryCreditStore, configure_shared_credit_service
from accounting_agents.credit_delivery import wire_shared_credit_service
from tests._fake_firestore import FakeFirestore
from tests.test_slack_runner import FakeSlackClient


@pytest.fixture(autouse=True)
def _credit_svc():
    configure_shared_credit_service(CreditService(InMemoryCreditStore()))
    wire_shared_credit_service()
    yield


def _client_store_without_firm(db: FakeFirestore):
    """Seed a client profile that has NO firm_id / slack_team_id."""
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
        # NOTE: deliberately no firm_id / slack_team_id.
    }
    db.collection("clients").document("c1").set(profile)
    db.collection("channels").document("C1").set({"client_id": "c1"})
    return FirestoreClientStore(client=db)


def test_require_firm_for_billing_reads_env(monkeypatch) -> None:
    monkeypatch.delenv("LEDGR_CREDIT_REQUIRE_FIRM", raising=False)
    assert require_firm_for_billing() is False
    monkeypatch.setenv("LEDGR_CREDIT_REQUIRE_FIRM", "1")
    assert require_firm_for_billing() is True


def test_flag_unresolved_firm_logs_error(caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="accounting_agents.credit_delivery"):
        flag_unresolved_firm_billing_anomaly(
            channel_id="C9", file_id="F9", source_filename="x.pdf"
        )
    assert any("billing anomaly" in rec.message for rec in caplog.records)
    assert any("C9" in str(rec.args) for rec in caplog.records)


def test_unresolved_firm_logs_loudly_and_processes_by_default(monkeypatch, caplog) -> None:
    monkeypatch.delenv("LEDGR_CREDIT_REQUIRE_FIRM", raising=False)
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    class _Runner:
        artifact_service = None
        session_service = None

    with caplog.at_level(logging.ERROR, logger="accounting_agents.credit_delivery"):
        with patch(
            "ledgr_agent.runtime.slack_shell.process_file_via_ledgr_agent",
            return_value={"status": "error", "channel_id": "C1", "file_id": "F-nofirm"},
        ):
            result = asyncio.run(
                process_file_event(
                    runner=_Runner(),
                    ledger_store=store,
                    db=db,
                    slack_client=slack,
                    channel_id="C1",
                    file_id="F-nofirm",
                    app_name="acc",
                    download_fn=lambda c, f: b"%PDF-1.4 fake",
                    source_filename="nofirm.pdf",
                    client_store=_client_store_without_firm(db),
                )
            )

    # Default policy: NOT blocked for missing firm; the upload proceeds.
    assert result.get("reason") != "no_firm"
    assert any("billing anomaly" in rec.message for rec in caplog.records)


def test_unresolved_firm_blocks_when_require_firm_set(monkeypatch) -> None:
    monkeypatch.setenv("LEDGR_CREDIT_REQUIRE_FIRM", "1")
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    class _Runner:
        artifact_service = None
        session_service = None

    with patch("ledgr_agent.runtime.slack_shell.read_doc") as mock_read:
        result = asyncio.run(
            process_file_event(
                runner=_Runner(),
                ledger_store=store,
                db=db,
                slack_client=slack,
                channel_id="C1",
                file_id="F-nofirm",
                app_name="acc",
                download_fn=lambda c, f: b"%PDF-1.4 fake",
                source_filename="nofirm.pdf",
                client_store=_client_store_without_firm(db),
            )
        )
        mock_read.assert_not_called()

    assert result["status"] == "blocked"
    assert result["reason"] == "no_firm"
    assert slack.uploads == []
