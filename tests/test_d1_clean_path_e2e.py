"""End-to-end wiring proof for the clean ``ledgr_agent`` path (issue #46 / Stream D, D1).

These tests exercise the three D1 pillars through the real Slack dispatch
(``process_file_event``) with ``LEDGR_USE_CLEAN_AGENT`` ON, asserting the gaps
the prior cutover tests did not cover:

1. Credits — durable + idempotent: re-processing the *same delivery* through the
   dispatch must NOT double-charge.
2. Delivery — the line-level provenance keys from #28 (``source_doc_id`` et al.)
   must NOT leak into the live ledger rows.
3. HITL — a pause that resumes via the ``hitl.py`` bridge on an *edit* decision
   delivers the edited rows (and stays idempotent on credits).

A final test proves the LEGACY graph path (flag OFF) is unaffected: the clean
``process_document_batch`` tool is never called and the legacy delivery still
fires. All tests are hermetic (fake Firestore / fake Slack / in-memory credit
store) so they run in the ``-m "not integration"`` set without real clients.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from accounting_agents import nodes
from accounting_agents.clean_agent_slack import handle_clean_agent_approval_action
from accounting_agents.credit_delivery import wire_shared_credit_service
from accounting_agents.hitl import write_interrupt
from accounting_agents.ledger_store import SlackLedgerStore
from accounting_agents.slack_runner import process_file_event
from app.credit_service import (
    CreditService,
    InMemoryCreditStore,
    configure_shared_credit_service,
)
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


@pytest.fixture
def credit_svc():
    """A fresh in-memory credit service wired as the shared singleton."""
    service = CreditService(InMemoryCreditStore())
    configure_shared_credit_service(service)
    wire_shared_credit_service()
    yield service


# --------------------------------------------------------------------------- #
# Stateful fakes (mirror the cutover tests so behaviour matches the live runner)
# --------------------------------------------------------------------------- #
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


class _CleanAgentRunner:
    def __init__(self) -> None:
        self.artifact_service = SimpleNamespace(save_artifact=lambda *a, **k: None)
        self.session_service = _StatefulSessionService()

    async def run_async(self, **kwargs):
        if False:
            yield None
        return


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


def _success_batch_with_provenance() -> dict:
    """A successful single-invoice batch whose export row carries #28 tags."""
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
                # Issue-#28 provenance tags — must be stripped before the ledger.
                "source_doc_id": "doc-abc-123",
                "tax_treatment": "SR",
                "account_code": "6010",
                "direction": "invoice",
            }
        ],
        "review_requests": [],
        "validation_summary": {},
        "credits": {"credit_status": "estimated"},
    }


# --------------------------------------------------------------------------- #
# Pillar 1 — credits are durable + idempotent through the full dispatch
# --------------------------------------------------------------------------- #
def test_clean_path_reprocess_same_delivery_does_not_double_charge(
    monkeypatch, credit_svc: CreditService
) -> None:
    monkeypatch.setenv("LEDGR_USE_CLEAN_AGENT", "1")
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    client_store = _seeded_store_with_firm(db, "TQA")

    credit_svc.ensure_firm("TQA")
    credit_svc.grant("TQA", 10, note="trial")

    def _run_once(runner):
        with patch(
            "ledgr_agent.tools.process_document_batch",
            return_value=_success_batch_with_provenance(),
        ):
            return asyncio.run(
                process_file_event(
                    runner=runner,
                    ledger_store=store,
                    db=db,
                    slack_client=slack,
                    channel_id="C1",
                    file_id="F-charge-once",
                    app_name="acc",
                    download_fn=lambda c, f: b"%PDF-1.4 fake",
                    source_filename="clean.pdf",
                    client_store=client_store,
                )
            )

    first = _run_once(_CleanAgentRunner())
    assert first["status"] == "delivered"
    assert credit_svc.read_balance("TQA") == 9, "first delivery should charge exactly 1"

    # Re-process the SAME Slack file (same channel + file_id → same idempotency
    # key). Credits must NOT be deducted a second time.
    second = _run_once(_CleanAgentRunner())
    assert second["status"] in {"delivered", "duplicate"}
    assert credit_svc.read_balance("TQA") == 9, "re-processing must not double-charge"


# --------------------------------------------------------------------------- #
# Pillar 2 — #28 provenance keys must NOT leak into the live ledger rows
# --------------------------------------------------------------------------- #
def test_clean_path_strips_issue28_provenance_from_ledger_rows(
    monkeypatch, credit_svc: CreditService
) -> None:
    monkeypatch.setenv("LEDGR_USE_CLEAN_AGENT", "1")
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    client_store = _seeded_store_with_firm(db, "TQA")

    credit_svc.ensure_firm("TQA")
    credit_svc.grant("TQA", 10, note="trial")

    captured: dict = {}
    real_append = store.append_rows

    def _spy_append(*args, **kwargs):
        captured["batches"] = kwargs.get("batches")
        return real_append(*args, **kwargs)

    with patch.object(store, "append_rows", side_effect=_spy_append):
        with patch(
            "ledgr_agent.tools.process_document_batch",
            return_value=_success_batch_with_provenance(),
        ):
            result = asyncio.run(
                process_file_event(
                    runner=_CleanAgentRunner(),
                    ledger_store=store,
                    db=db,
                    slack_client=slack,
                    channel_id="C1",
                    file_id="F-prov",
                    app_name="acc",
                    download_fn=lambda c, f: b"%PDF-1.4 fake",
                    source_filename="clean.pdf",
                    client_store=client_store,
                )
            )

    assert result["status"] == "delivered"
    batches = captured.get("batches") or []
    assert batches, "expected ledger batches to be appended"
    leaked: set[str] = set()
    for batch in batches:
        for row in batch.get("rows") or []:
            leaked |= {
                "source_doc_id",
                "tax_treatment",
                "account_code",
                "direction",
                "workbook",
                "sheet",
            } & set(row)
    assert not leaked, f"provenance/export keys leaked into ledger rows: {sorted(leaked)}"
    # The human-facing column must still be present.
    assert any(
        "Invoice Number" in row
        for batch in batches
        for row in (batch.get("rows") or [])
    )


# --------------------------------------------------------------------------- #
# Pillar 3 — HITL pause → Slack EDIT → resume delivers the edited rows
# --------------------------------------------------------------------------- #
def test_clean_path_hitl_edit_resume_delivers_edited_rows(
    credit_svc: CreditService,
) -> None:
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    runner = _CleanAgentRunner()
    client_store = _seeded_store_with_firm(db, "TQA")

    credit_svc.ensure_firm("TQA")
    credit_svc.grant("TQA", 10, note="trial")

    payload = {
        "client_id": "c1",
        "client_name": "Test Client",
        "fy": "2026",
        "kind": "invoice",
        "software": "QBS Ledger",
        "file_id": "F-edit",
        "batches": [
            {
                "sheet": "Purchase",
                "doc_key": "Purchase:INV-EDIT",
                "rows": [
                    {
                        "Invoice Number": "INV-EDIT",
                        "Description": "Widget",
                        "Amount": 12.0,
                    }
                ],
            }
        ],
    }
    write_interrupt(
        db,
        "C1:F-edit",
        session_id="C1:F-edit",
        channel_id="C1",
        slack_file_id="F-edit",
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
            op_id="C1:F-edit",
            decision="edit",
            app_name="acc",
            edits={"lines": [{"index": 0, "net_amount": 99.0}]},
            client_store=client_store,
        )
    )

    assert result["status"] == "resumed"
    assert result["decision"] == "edit"
    # The edited amount must reach the ledger.
    session = asyncio.run(
        runner.session_service.get_session(
            app_name="acc", user_id="C1", session_id="C1:F-edit"
        )
    )
    rows = session.state[nodes.LEDGER_ROWS_KEY]["batches"][0]["rows"]
    assert rows[0]["Amount"] == 99.0
    assert len(slack.uploads) == 1


# --------------------------------------------------------------------------- #
# Legacy path (flag OFF) is UNAFFECTED — clean tool never called, graph delivers
# --------------------------------------------------------------------------- #
def test_legacy_graph_path_unaffected_when_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("LEDGR_USE_CLEAN_AGENT", raising=False)
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    # Re-use the legacy graph harness from test_slack_runner.
    from tests.test_slack_runner import _FakeRunner, _ledger_payload

    final_event = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text="done")]),
        get_function_calls=lambda: [],
    )
    runner = _FakeRunner([final_event], _ledger_payload())

    with patch("ledgr_agent.tools.process_document_batch") as clean_tool:
        result = asyncio.run(
            process_file_event(
                runner=runner,
                ledger_store=store,
                db=db,
                slack_client=slack,
                channel_id="C1",
                file_id="F1",
                app_name="acc",
                download_fn=lambda c, f: b"%PDF-1.4 fake",
                source_filename="invoice.pdf",
                client_store=_seeded_client_store(db),
            )
        )
        # The clean-agent tool must NEVER be reached on the legacy path.
        clean_tool.assert_not_called()

    assert result["status"] == "delivered"
    assert len(slack.uploads) == 1
    assert result["append"]["appended"] == 1
