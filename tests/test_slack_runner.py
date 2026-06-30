"""Hermetic tests for the Slack file path (ledgr_agent via slack_shell).

Covers pure slack_runner helpers and ``process_file_event`` on the lean
read_doc → build_sheets → deliver path. Legacy graph HITL / approve-resume
tests were removed — ledgr v1 has no mid-flow pause.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ledgr_slack.ledger_store import SlackLedgerStore
from ledgr_slack.app import (
    _derive_setup_prefill,
    build_fastapi_app,
    deslugify_channel_name,
)
from ledgr_slack.file_event import process_file_event
from app.native_blocks_compat import _reset_for_tests
from tests._fake_firestore import FakeFirestore
from tests._slack_test_helpers import (
    _FakeRunner,
    ledger_payload,
    posted_texts,
    seeded_client_store,
)
from tests.test_ledger_store import FakeSlackClient


LEDGER_ROWS_KEY = "ledger_rows"


@pytest.fixture(autouse=True)
def _force_fallback_blocks(monkeypatch):
    monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "0")
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.fixture(autouse=True)
def _autouse_mock_ledgr_read_doc(mock_ledgr_read_doc):
    yield


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_profile_state_delta_includes_software():
    from ledgr_slack.client_store import _profile_state_delta
    from ledgr_slack.client_context import ClientContext

    class _Store:
        def get_by_channel(self, channel_id):
            assert channel_id == "C1"
            return ClientContext(
                client_id="CL-1",
                client_name="Company-A",
                accounting_software="Xero",
                fye_month=10,
            )

    delta = _profile_state_delta(_Store(), "C1")
    assert delta["software"] == "Xero"
    assert delta["client_id"] == "CL-1"


def test_profile_state_delta_empty_when_no_profile():
    from ledgr_slack.client_store import _profile_state_delta

    class _Store:
        def get_by_channel(self, channel_id):
            return None

    assert _profile_state_delta(_Store(), "C1") == {}


# ---------------------------------------------------------------------------
# process_file_event (ledgr path)
# ---------------------------------------------------------------------------


def test_process_file_event_softgates_when_no_profile():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    class _NoProfileStore:
        def get_by_channel(self, channel_id):
            return None

    runner = _FakeRunner([], ledger_payload())
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
            client_store=_NoProfileStore(),
        )
    )
    assert result["status"] == "no_profile"
    assert any("this client set up" in t.lower() for t in posted_texts(slack))


def test_process_file_event_completion_appends_ledger_once():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    final_event = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text="done")]),
        get_function_calls=lambda: [],
    )
    runner = _FakeRunner([final_event], ledger_payload())

    downloaded: dict = {}

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
            client_store=seeded_client_store(db),
        )
    )

    assert result["status"] == "delivered"
    from ledgr_agent.internal.uploads import artifact_name_for

    expected_artifact = artifact_name_for("F1")
    assert ("C1", expected_artifact) in runner.artifact_service.saved
    assert downloaded["file_id"] == "F1"
    assert len(slack.uploads) == 1
    assert result["delivery"]["append"]["appended"] == 1
    assert any("FY2026" in u for u in posted_texts(slack))


def test_process_file_event_defer_slack_delivery_writes_processing_log():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    custom_payload = ledger_payload()
    custom_payload[LEDGER_ROWS_KEY]["file_id"] = "F-batch-1"
    custom_payload["source_filename"] = "25-D15-Company-A.pdf"
    runner = _FakeRunner([], custom_payload)

    written: list[dict] = []

    class _RecordingClientStore:
        def __init__(self, _db):
            self._db = _db

        def append_processing_log(self, *, client_id, file_id, entry):
            written.append({"client_id": client_id, "file_id": file_id, "entry": entry})

        def get_by_channel(self, _channel_id):
            from ledgr_slack.client_context import ClientContext

            return ClientContext(
                client_id="c1",
                client_name="Test",
                accounting_software="QBS",
                fye_month=12,
                channel_id="C1",
            )

    rec_store = _RecordingClientStore(db)

    asyncio.run(
        process_file_event(
            runner=runner,
            ledger_store=store,
            db=db,
            slack_client=slack,
            channel_id="C1",
            file_id="F-batch-1",
            app_name="acc",
            download_fn=lambda c, f: b"%PDF-1.4 fake",
            source_filename="25-D15-Company-A.pdf",
            client_store=rec_store,
            defer_slack_delivery=True,
        )
    )

    assert len(written) == 1
    entry = written[0]["entry"]
    assert entry["file_id"] == "F-batch-1"
    assert entry["filename"] == "25-D15-Company-A.pdf"
    assert entry["fy"] == "2026"
    assert entry["row_count"] == 1
    assert "delivery_message_ts" not in entry


def test_process_file_event_completion_writes_processing_log_with_delivery_ts():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    custom_payload = ledger_payload()
    custom_payload[LEDGER_ROWS_KEY]["file_id"] = "F-clean-1"
    custom_payload["source_filename"] = "clean.pdf"
    runner = _FakeRunner([], custom_payload)

    written: list[dict] = []

    class _RecordingClientStore:
        def append_processing_log(self, *, client_id, file_id, entry):
            written.append(entry)

        def get_by_channel(self, _channel_id):
            from ledgr_slack.client_context import ClientContext

            return ClientContext(
                client_id="c1",
                client_name="Test",
                accounting_software="QBS",
                fye_month=12,
                channel_id="C1",
            )

    rec_store = _RecordingClientStore()

    asyncio.run(
        process_file_event(
            runner=runner,
            ledger_store=store,
            db=db,
            slack_client=slack,
            channel_id="C1",
            file_id="F-clean-1",
            app_name="acc",
            download_fn=lambda c, f: b"%PDF-1.4 fake",
            source_filename="clean.pdf",
            client_store=rec_store,
            thread_ts="1716000000.000200",
        )
    )

    assert len(written) == 1
    assert written[0]["delivery_message_ts"] == "1716000000.000200"
    assert written[0]["channel_id"] == "C1"


def test_process_file_event_rejects_empty_bytes():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    runner = _FakeRunner([], ledger_payload())

    result = asyncio.run(
        process_file_event(
            runner=runner,
            ledger_store=store,
            db=db,
            slack_client=slack,
            channel_id="C1",
            file_id="F1",
            app_name="acc",
            download_fn=lambda c, f: b"",
            source_filename="mystery.bin",
            client_store=seeded_client_store(db),
        )
    )

    assert result["status"] == "rejected_unreadable"
    texts = posted_texts(slack)
    assert any(
        "empty" in t.lower() or "supported" in t.lower() or "couldn't read" in t.lower()
        for t in texts
    )
    assert runner.artifact_service.saved == {}
    assert slack.uploads == []


def test_process_file_event_rejects_unknown_extension():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())
    runner = _FakeRunner([], ledger_payload())

    result = asyncio.run(
        process_file_event(
            runner=runner,
            ledger_store=store,
            db=db,
            slack_client=slack,
            channel_id="C1",
            file_id="F1",
            app_name="acc",
            download_fn=lambda c, f: b"\x4d\x5a\x90\x00" * 10,
            source_filename="setup.exe",
            client_store=seeded_client_store(db),
        )
    )

    assert result["status"] == "rejected_unreadable"
    texts = posted_texts(slack)
    assert any(
        "empty" in t.lower() or "supported" in t.lower() or "couldn't read" in t.lower()
        for t in texts
    )
    assert runner.artifact_service.saved == {}
    assert slack.uploads == []


def test_process_file_event_accepted_pdf_still_processes():
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(FakeFirestore(), opener=slack.opener())

    final_event = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text="done")]),
        get_function_calls=lambda: [],
    )
    runner = _FakeRunner([final_event], ledger_payload())

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
            client_store=seeded_client_store(db),
        )
    )

    assert result["status"] == "delivered"
    assert len(slack.uploads) == 1


# ---------------------------------------------------------------------------
# Channel setup helpers
# ---------------------------------------------------------------------------


def test_deslugify_channel_name_basic():
    assert deslugify_channel_name("sample-channel-client-pte-ltd") == "Sample Channel Client Pte Ltd"


def test_deslugify_channel_name_underscores_and_suffixes():
    assert deslugify_channel_name("foo_bar_llp") == "Foo Bar LLP"
    assert deslugify_channel_name("acme-sg-pte-ltd") == "Acme SG Pte Ltd"


def test_deslugify_channel_name_empty():
    assert deslugify_channel_name("") == ""
    assert deslugify_channel_name("---") == ""


def test_derive_setup_prefill_from_channel_name():
    class _InfoClient:
        def conversations_info(self, *, channel):
            assert channel == "C-OPEN"
            return {"ok": True, "channel": {"id": channel, "name": "sample-channel-client-pte-ltd"}}

    body = {"channel": {"id": "C-OPEN"}, "trigger_id": "t1"}
    prefill = asyncio.run(_derive_setup_prefill(_InfoClient(), body))
    assert prefill == {"client_name": "Sample Channel Client Pte Ltd"}


def test_derive_setup_prefill_handles_lookup_failure():
    class _BoomClient:
        def conversations_info(self, *, channel):
            raise RuntimeError("missing_scope")

    body = {"channel": {"id": "C-OPEN"}}
    assert asyncio.run(_derive_setup_prefill(_BoomClient(), body)) is None


def test_derive_setup_prefill_no_channel_returns_none():
    assert asyncio.run(_derive_setup_prefill(object(), {})) is None


def test_resolve_file_name_prefers_file_object_name():
    from ledgr_slack.ux import _resolve_file_name

    class _Client:
        def files_info(self, file):
            raise AssertionError("must not call files_info when the name is present")

    assert _resolve_file_name(_Client(), "F1", {"name": "Invoice-99.pdf"}) == "Invoice-99.pdf"


def test_resolve_file_name_falls_back_to_files_info():
    from ledgr_slack.ux import _resolve_file_name

    class _Resp:
        data = {"file": {"name": "scan.exe"}}

    class _Client:
        def files_info(self, file):
            return _Resp()

    assert _resolve_file_name(_Client(), "F1", None) == "scan.exe"


def test_resolve_file_name_defaults_only_when_truly_unavailable():
    from ledgr_slack.ux import _resolve_file_name

    class _Client:
        def files_info(self, file):
            raise RuntimeError("boom")

    assert _resolve_file_name(_Client(), "F1", None) == "document.pdf"


# ---------------------------------------------------------------------------
# FastAPI wiring smoke
# ---------------------------------------------------------------------------


def test_build_fastapi_app_wires_adk_graph(monkeypatch):
    import ledgr_slack.app as _app_mod
    import ledgr_slack.sessions as _sessions_mod
    import ledgr_slack.client_context as _ctx_mod
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

    monkeypatch.setattr(_app_mod, "build_runner", _fake_build_runner)
    monkeypatch.setattr(_app_mod, "build_async_app", _fake_build_async_app)
    monkeypatch.setattr(_app_mod, "SlackLedgerStore", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(_sessions_mod, "FirestoreSessionService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(_ctx_mod, "FirestoreClientStore", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(_handler_mod, "AsyncSlackRequestHandler", MagicMock(return_value=fake_handler))

    api = build_fastapi_app()
    assert isinstance(api, FastAPI)
    paths = {r.path for r in api.routes}
    assert "/slack/events" in paths
    assert "/healthz" in paths

    tc = TestClient(api, raise_server_exceptions=False)
    tc.post("/slack/events")

    assert len(runner_calls) == 1
    assert len(app_calls) == 1
    assert app_calls[0]["runner"] is not None
    assert app_calls[0]["store"] is not None
