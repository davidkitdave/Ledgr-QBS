"""Tests for COA ingest — hermetic, no live Slack/Firestore/Gemini calls."""

from __future__ import annotations

import csv
import os
import tempfile
from typing import Any
from unittest.mock import patch

import openpyxl
import pytest

from app.coa_ingest import CoaIngestOutcome, coa_rows_from_file, ingest_coa, standard_coa_rows
from invoice_processing.export.client_context import ClientContext, InMemoryClientStore


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_COA_HEADERS = [
    "Account code", "Description", "Account type",
    "Financial Statement", "Nature", "AI Search Keywords",
]

_SAMPLE_ROWS = [
    ("4-1000", "Sales / Revenue", "Income", "Profit & Loss", "Credit", "sales revenue"),
    ("",       "Other Income",    "Income", "Profit & Loss", "Credit", "misc income"),
    ("6-1000", "Salaries & Wages","Expense","Profit & Loss", "Debit",  "salary wages"),
]


def _make_xlsx_with_coa_sheet(path: str) -> None:
    """Write an xlsx with a sheet named 'COA' containing _SAMPLE_ROWS."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "COA"
    ws.append(_COA_HEADERS)
    for row in _SAMPLE_ROWS:
        ws.append(list(row))
    wb.save(path)


def _make_xlsx_no_coa_sheet(path: str) -> None:
    """Write an xlsx whose only sheet is NOT named 'COA'."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Accounts"   # NOT 'COA' — triggers fallback
    ws.append(_COA_HEADERS)
    for row in _SAMPLE_ROWS:
        ws.append(list(row))
    wb.save(path)


def _make_csv(path: str) -> None:
    """Write a CSV with the spec headers and _SAMPLE_ROWS."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_COA_HEADERS)
        for row in _SAMPLE_ROWS:
            writer.writerow(list(row))


def _pending_store(channel_id: str = "C-CHAN-1") -> InMemoryClientStore:
    """InMemoryClientStore with one pending_coa client wired to channel_id."""
    store = InMemoryClientStore()
    store.save_profile({
        "client_id": "cli-001",
        "client_name": "Test Co",
        "channel_id": channel_id,
        "fye_month": 12,
        "region": "SINGAPORE",
        "accounting_software": "QBS Ledger",
        "base_currency": "SGD",
        "gst_registered": True,
        "status": "pending_coa",
    })
    return store


# --------------------------------------------------------------------------- #
# coa_rows_from_file — xlsx with COA sheet
# --------------------------------------------------------------------------- #

class TestCoaRowsFromXlsxWithCoaSheet:

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        self.tmp.close()
        _make_xlsx_with_coa_sheet(self.tmp.name)

    def teardown_method(self):
        os.unlink(self.tmp.name)

    def test_returns_list(self):
        rows = coa_rows_from_file(self.tmp.name)
        assert isinstance(rows, list)

    def test_correct_count(self):
        rows = coa_rows_from_file(self.tmp.name)
        assert len(rows) == len(_SAMPLE_ROWS)

    def test_first_row_code(self):
        rows = coa_rows_from_file(self.tmp.name)
        assert rows[0]["code"] == "4-1000"

    def test_first_row_description(self):
        rows = coa_rows_from_file(self.tmp.name)
        assert rows[0]["description"] == "Sales / Revenue"

    def test_blank_code_row_preserved(self):
        """Row with blank Account code but valid Description must be kept."""
        rows = coa_rows_from_file(self.tmp.name)
        blank_code_rows = [r for r in rows if r["code"] == ""]
        assert len(blank_code_rows) == 1
        assert blank_code_rows[0]["description"] == "Other Income"

    def test_all_six_keys_present(self):
        rows = coa_rows_from_file(self.tmp.name)
        expected = {"code", "description", "account_type", "financial_statement", "nature", "keywords"}
        for row in rows:
            assert expected.issubset(row.keys()), f"Missing keys in {row}"


# --------------------------------------------------------------------------- #
# coa_rows_from_file — csv
# --------------------------------------------------------------------------- #

class TestCoaRowsFromCsv:

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w")
        self.tmp.close()
        _make_csv(self.tmp.name)

    def teardown_method(self):
        os.unlink(self.tmp.name)

    def test_returns_correct_count(self):
        rows = coa_rows_from_file(self.tmp.name)
        assert len(rows) == len(_SAMPLE_ROWS)

    def test_first_row_code(self):
        rows = coa_rows_from_file(self.tmp.name)
        assert rows[0]["code"] == "4-1000"

    def test_blank_code_row_preserved(self):
        rows = coa_rows_from_file(self.tmp.name)
        blank_code_rows = [r for r in rows if r["code"] == ""]
        assert len(blank_code_rows) == 1

    def test_all_six_keys_present(self):
        rows = coa_rows_from_file(self.tmp.name)
        expected = {"code", "description", "account_type", "financial_statement", "nature", "keywords"}
        for row in rows:
            assert expected.issubset(row.keys())


# --------------------------------------------------------------------------- #
# coa_rows_from_file — xlsx WITHOUT a COA sheet (fallback path)
# --------------------------------------------------------------------------- #

class TestCoaRowsFromXlsxFallback:

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        self.tmp.close()
        _make_xlsx_no_coa_sheet(self.tmp.name)

    def teardown_method(self):
        os.unlink(self.tmp.name)

    def test_fallback_reads_first_sheet(self):
        rows = coa_rows_from_file(self.tmp.name)
        assert len(rows) == len(_SAMPLE_ROWS)

    def test_fallback_first_row_code(self):
        rows = coa_rows_from_file(self.tmp.name)
        assert rows[0]["code"] == "4-1000"

    def test_fallback_all_six_keys(self):
        rows = coa_rows_from_file(self.tmp.name)
        expected = {"code", "description", "account_type", "financial_statement", "nature", "keywords"}
        for row in rows:
            assert expected.issubset(row.keys())


# --------------------------------------------------------------------------- #
# standard_coa_rows
# --------------------------------------------------------------------------- #

class TestStandardCoaRows:

    def test_returns_at_least_16(self):
        rows = standard_coa_rows()
        assert len(rows) >= 16

    def test_each_row_has_six_keys(self):
        rows = standard_coa_rows()
        expected = {"code", "description", "account_type", "financial_statement", "nature", "keywords"}
        for row in rows:
            assert expected.issubset(row.keys()), f"Missing keys in {row}"

    def test_all_descriptions_non_empty(self):
        rows = standard_coa_rows()
        for row in rows:
            assert row["description"].strip(), f"Empty description in {row}"


# --------------------------------------------------------------------------- #
# ingest_coa — active path
# --------------------------------------------------------------------------- #

class TestIngestCoaActive:

    def setup_method(self):
        self.channel_id = "C-CHAN-1"
        self.store = _pending_store(self.channel_id)
        self.posted: list[dict] = []
        self.rows = standard_coa_rows()

        def _say(**kwargs):
            self.posted.append(kwargs)

        self.outcome = ingest_coa(
            channel_id=self.channel_id,
            store=self.store,
            rows=self.rows,
            say_fn=_say,
        )

    def test_outcome_status_active(self):
        assert self.outcome.status == "active"

    def test_outcome_n_accounts(self):
        assert self.outcome.n_accounts == len(self.rows)

    def test_outcome_client_id_set(self):
        assert self.outcome.client_id == "cli-001"

    def test_store_status_active(self):
        ctx = self.store.get_by_channel(self.channel_id)
        assert ctx.status == "active"

    def test_store_coa_populated(self):
        ctx = self.store.get_by_channel(self.channel_id)
        assert len(ctx.coa) == len(self.rows)

    def test_confirmation_posted(self):
        assert len(self.posted) == 1

    def test_confirmation_has_blocks(self):
        assert "blocks" in self.posted[0]

    def test_confirmation_mentions_account_count(self):
        """The confirmation message must reference the account count."""
        blocks = self.posted[0]["blocks"]
        text = str(blocks)
        assert str(len(self.rows)) in text


# --------------------------------------------------------------------------- #
# ingest_coa — no_profile path
# --------------------------------------------------------------------------- #

class TestIngestCoaNoProfile:

    def test_no_profile_status(self):
        store = InMemoryClientStore()  # empty — no client registered
        posted: list[dict] = []
        outcome = ingest_coa(
            channel_id="C-UNKNOWN",
            store=store,
            rows=standard_coa_rows(),
            say_fn=lambda **kw: posted.append(kw),
        )
        assert outcome.status == "no_profile"

    def test_no_profile_client_id_none(self):
        store = InMemoryClientStore()
        outcome = ingest_coa(
            channel_id="C-UNKNOWN",
            store=store,
            rows=standard_coa_rows(),
            say_fn=lambda **kw: None,
        )
        assert outcome.client_id is None

    def test_no_profile_posts_setup_prompt(self):
        store = InMemoryClientStore()
        posted: list[dict] = []
        ingest_coa(
            channel_id="C-UNKNOWN",
            store=store,
            rows=standard_coa_rows(),
            say_fn=lambda **kw: posted.append(kw),
        )
        assert len(posted) == 1
        assert "blocks" in posted[0]


# --------------------------------------------------------------------------- #
# ingest_coa — empty rows path
# --------------------------------------------------------------------------- #

class TestIngestCoaEmpty:

    def test_empty_rows_status(self):
        store = _pending_store("C-CHAN-2")
        outcome = ingest_coa(
            channel_id="C-CHAN-2",
            store=store,
            rows=[],
            say_fn=lambda **kw: None,
        )
        assert outcome.status == "empty"

    def test_empty_rows_message_posted(self):
        store = _pending_store("C-CHAN-2")
        posted: list[dict] = []
        ingest_coa(
            channel_id="C-CHAN-2",
            store=store,
            rows=[],
            say_fn=lambda **kw: posted.append(kw),
        )
        assert len(posted) == 1


# --------------------------------------------------------------------------- #
# handle_file_share disambiguation
# --------------------------------------------------------------------------- #

class FakeAck:
    def __init__(self):
        self.called = False

    def __call__(self, *a, **kw):
        self.called = True


class FakeClient:
    def __init__(self):
        self.posted_messages: list[dict] = []

    def chat_postMessage(self, **kwargs):
        self.posted_messages.append(kwargs)
        return {"ok": True}

    def files_info(self, file):
        return {"file": {"url_private_download": "http://example.com/f", "name": f"{file}.pdf"}}

    @property
    def token(self):
        return "xoxb-fake"


class TestHandleFileShareDisambiguation:
    """Tests for file-type routing in handle_file_share."""

    def _run_share_calls(self):
        return []

    def test_xlsx_routes_to_run_coa_ingest_not_run_share(self):
        from app import slack_app

        coa_calls: list[dict] = []
        share_calls: list[dict] = []

        def fake_run_coa(**kw):
            coa_calls.append(kw)

        def fake_run_share(**kw):
            share_calls.append(kw)

        event = {
            "channel": "C-TEST",
            "files": [{"id": "F001", "filetype": "xlsx", "name": "coa.xlsx"}],
        }
        client = FakeClient()
        store = _pending_store("C-TEST")

        with patch.object(slack_app, "run_coa_ingest", fake_run_coa), \
             patch.object(slack_app, "run_share", fake_run_share), \
             patch.object(slack_app, "slack_download_file", return_value="/tmp/coa.xlsx"), \
             patch.object(slack_app._executor, "submit", lambda fn, *a, **kw: fn(*a, **kw)):
            slack_app.handle_file_share(event, client, store)

        assert len(coa_calls) == 1
        assert len(share_calls) == 0

    def test_pdf_routes_to_run_share_not_run_coa_ingest(self):
        from app import slack_app

        coa_calls: list[dict] = []
        share_calls: list[dict] = []

        def fake_run_coa(**kw):
            coa_calls.append(kw)

        def fake_run_share(**kw):
            share_calls.append(kw)

        event = {
            "channel": "C-TEST",
            "files": [{"id": "F002", "filetype": "pdf", "name": "invoice.pdf"}],
        }
        client = FakeClient()
        store = _pending_store("C-TEST")

        with patch.object(slack_app, "run_coa_ingest", fake_run_coa), \
             patch.object(slack_app, "run_share", fake_run_share), \
             patch.object(slack_app, "slack_download_file", return_value="/tmp/invoice.pdf"), \
             patch.object(slack_app._executor, "submit", lambda fn, *a, **kw: fn(*a, **kw)):
            slack_app.handle_file_share(event, client, store)

        assert len(coa_calls) == 0
        assert len(share_calls) == 1

    def test_mixed_message_triggers_both(self):
        from app import slack_app

        coa_calls: list[dict] = []
        share_calls: list[dict] = []

        def fake_run_coa(**kw):
            coa_calls.append(kw)

        def fake_run_share(**kw):
            share_calls.append(kw)

        event = {
            "channel": "C-TEST",
            "files": [
                {"id": "F003", "filetype": "xlsx", "name": "coa.xlsx"},
                {"id": "F004", "filetype": "pdf", "name": "invoice.pdf"},
            ],
        }
        client = FakeClient()
        store = _pending_store("C-TEST")

        with patch.object(slack_app, "run_coa_ingest", fake_run_coa), \
             patch.object(slack_app, "run_share", fake_run_share), \
             patch.object(slack_app, "slack_download_file", return_value="/tmp/file"), \
             patch.object(slack_app._executor, "submit", lambda fn, *a, **kw: fn(*a, **kw)):
            slack_app.handle_file_share(event, client, store)

        assert len(coa_calls) == 1
        assert len(share_calls) == 1

    def test_bot_message_ignored(self):
        from app import slack_app

        coa_calls: list[dict] = []
        share_calls: list[dict] = []

        event = {
            "channel": "C-TEST",
            "bot_id": "B-BOT",
            "files": [{"id": "F005", "filetype": "xlsx", "name": "coa.xlsx"}],
        }
        client = FakeClient()
        store = _pending_store("C-TEST")

        with patch.object(slack_app, "run_coa_ingest", lambda **kw: coa_calls.append(kw)), \
             patch.object(slack_app, "run_share", lambda **kw: share_calls.append(kw)), \
             patch.object(slack_app._executor, "submit", lambda fn, *a, **kw: fn(*a, **kw)):
            slack_app.handle_file_share(event, client, store)

        assert len(coa_calls) == 0
        assert len(share_calls) == 0

    def test_csv_by_name_extension_routes_to_coa(self):
        """filetype may be 'text' for CSV; extension fallback must catch it."""
        from app import slack_app

        coa_calls: list[dict] = []

        event = {
            "channel": "C-TEST",
            "files": [{"id": "F006", "filetype": "text", "name": "coa.csv"}],
        }
        client = FakeClient()
        store = _pending_store("C-TEST")

        with patch.object(slack_app, "run_coa_ingest", lambda **kw: coa_calls.append(kw)), \
             patch.object(slack_app, "run_share", lambda **kw: None), \
             patch.object(slack_app, "slack_download_file", return_value="/tmp/coa.csv"), \
             patch.object(slack_app._executor, "submit", lambda fn, *a, **kw: fn(*a, **kw)):
            slack_app.handle_file_share(event, client, store)

        assert len(coa_calls) == 1


# --------------------------------------------------------------------------- #
# handle_use_standard_coa
# --------------------------------------------------------------------------- #

class TestHandleUseStandardCoa:

    def _run(self, channel_id: str = "C-STD-1"):
        from app.slack_app import handle_use_standard_coa

        store = _pending_store(channel_id)
        ack = FakeAck()
        client = FakeClient()
        body = {
            "container": {"channel_id": channel_id},
        }
        handle_use_standard_coa(body, ack, client, store)
        return store, ack, client

    def test_acks(self):
        _, ack, _ = self._run()
        assert ack.called

    def test_client_status_becomes_active(self):
        store, _, _ = self._run("C-STD-1")
        ctx = store.get_by_channel("C-STD-1")
        assert ctx.status == "active"

    def test_coa_populated_with_standard_rows(self):
        store, _, _ = self._run("C-STD-1")
        ctx = store.get_by_channel("C-STD-1")
        assert len(ctx.coa) == len(standard_coa_rows())

    def test_confirmation_message_posted(self):
        _, _, client = self._run("C-STD-1")
        assert len(client.posted_messages) == 1

    def test_confirmation_message_channel(self):
        _, _, client = self._run("C-STD-1")
        assert client.posted_messages[0]["channel"] == "C-STD-1"

    def test_confirmation_has_blocks(self):
        _, _, client = self._run("C-STD-1")
        assert "blocks" in client.posted_messages[0]
