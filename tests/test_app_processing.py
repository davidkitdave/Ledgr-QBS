"""Hermetic tests for app/processing.py, app/blocks.py (new builders),
and app/slack_app.py handle_file_share.

No network, no Gemini, no live Slack token.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from app.blocks import needs_setup_blocks, result_card
from app.processing import ShareOutcome, process_shared_files
from invoice_processing.export.client_context import ClientContext, InMemoryClientStore
from invoice_processing.pipeline import BatchResult, ProcessedDoc


# --------------------------------------------------------------------------- #
# Shared fakes / fixtures
# --------------------------------------------------------------------------- #

def _make_client(*, status: str = "active") -> ClientContext:
    return ClientContext(
        client_id="client-test-1",
        client_name="TestCo Pte Ltd",
        channel_id="C-TEST",
        status=status,
        fye_month=12,
        accounting_software="QBS Ledger",
    )


def _make_store(client: Optional[ClientContext] = None, channel_id: str = "C-TEST") -> InMemoryClientStore:
    store = InMemoryClientStore()
    if client is not None:
        store.add(client, channel_id=channel_id)
    return store


def _stub_route() -> DocRoute:
    from invoice_processing.export.routing import DocRoute
    return DocRoute(
        fy=2025,
        bucket="purchase",
        archive_path="client-test-1/FY2025/purchase/doc.pdf",
        workbook="Ledger_FY2025.xlsx",
        sheet="Purchase",
    )


def _make_batch_result(n_ok: int = 2) -> BatchResult:
    """A BatchResult with one workbook and n_ok processed docs."""
    ok_note = "ok"
    docs = [
        ProcessedDoc(
            path=f"/tmp/doc{i}.pdf",
            doc_type="invoice",
            direction="purchase",
            normalized=None,
            bank=None,
            route=_stub_route(),
            reconciled=True,
            note=ok_note,
        )
        for i in range(n_ok)
    ]
    return BatchResult(
        workbooks={"Ledger_FY2025.xlsx": b"PK\x03\x04"},
        docs=docs,
        errors=[],
    )


# --------------------------------------------------------------------------- #
# Test: happy path — active client, one workbook, two ok docs
# --------------------------------------------------------------------------- #

class TestProcessSharedFilesHappyPath:

    def setup_method(self):
        self.client = _make_client(status="active")
        self.store = _make_store(self.client)
        self.uploaded: list[tuple] = []
        self.posted: list[dict] = []

        # Create real temp files so cleanup can be verified
        self._tmp_files: list[str] = []
        for i in range(2):
            fd, path = tempfile.mkstemp(suffix=f"_doc{i}.pdf")
            os.close(fd)
            self._tmp_files.append(path)

        file_iter = iter(self._tmp_files)

        def _download(fid: str) -> str:
            return next(file_iter)

        def _upload(channel_id: str, filename: str, data: bytes, title: str) -> None:
            self.uploaded.append((channel_id, filename, data, title))

        def _say(**kwargs) -> None:
            self.posted.append(kwargs)

        self.outcome = process_shared_files(
            channel_id="C-TEST",
            file_ids=["F001", "F002"],
            store=self.store,
            download_fn=_download,
            upload_fn=_upload,
            say_fn=_say,
            pipeline_fn=lambda paths, client: _make_batch_result(n_ok=2),
        )

    def teardown_method(self):
        for p in self._tmp_files:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    def test_status_ok(self):
        assert self.outcome.status == "ok"

    def test_n_processed(self):
        assert self.outcome.n_processed == 2

    def test_workbooks_list(self):
        assert self.outcome.workbooks == ["Ledger_FY2025.xlsx"]

    def test_no_errors(self):
        assert self.outcome.errors == []

    def test_upload_called_once(self):
        assert len(self.uploaded) == 1

    def test_upload_channel_id(self):
        assert self.uploaded[0][0] == "C-TEST"

    def test_upload_filename(self):
        assert self.uploaded[0][1] == "Ledger_FY2025.xlsx"

    def test_upload_bytes(self):
        assert self.uploaded[0][2] == b"PK\x03\x04"

    def test_result_card_posted(self):
        assert len(self.posted) == 1

    def test_result_card_has_blocks(self):
        assert "blocks" in self.posted[0]

    def test_result_card_blocks_is_list(self):
        assert isinstance(self.posted[0]["blocks"], list)

    def test_result_card_no_coa_missing_note(self):
        # active client → no COA-missing context block
        blocks = self.posted[0]["blocks"]
        texts = [
            str(b) for b in blocks
        ]
        combined = " ".join(texts)
        assert "No COA" not in combined


# --------------------------------------------------------------------------- #
# Test: no-profile channel
# --------------------------------------------------------------------------- #

class TestNoProfile:

    def setup_method(self):
        self.store = _make_store(None)  # empty store
        self.posted: list[dict] = []
        self.pipeline_called = False

        def _pipeline(paths, client):
            self.pipeline_called = True
            return _make_batch_result()

        self.outcome = process_shared_files(
            channel_id="C-UNKNOWN",
            file_ids=["F001"],
            store=self.store,
            download_fn=lambda fid: "/tmp/x.pdf",
            upload_fn=lambda *a, **kw: None,
            say_fn=lambda **kw: self.posted.append(kw),
            pipeline_fn=_pipeline,
        )

    def test_status_no_profile(self):
        assert self.outcome.status == "no_profile"

    def test_pipeline_not_called(self):
        assert not self.pipeline_called

    def test_needs_setup_posted(self):
        assert len(self.posted) == 1
        blocks = self.posted[0].get("blocks", [])
        assert isinstance(blocks, list)
        assert len(blocks) > 0

    def test_needs_setup_has_button(self):
        blocks = self.posted[0]["blocks"]
        action_block = next((b for b in blocks if b.get("type") == "actions"), None)
        assert action_block is not None
        button = action_block["elements"][0]
        assert button["action_id"] == "ledgr_setup_open"


# --------------------------------------------------------------------------- #
# Test: coa_missing note when client status != "active"
# --------------------------------------------------------------------------- #

class TestCoaMissingNote:

    def test_coa_missing_in_result_card(self):
        client = _make_client(status="pending_coa")
        store = _make_store(client)
        posted: list[dict] = []

        process_shared_files(
            channel_id="C-TEST",
            file_ids=["F001"],
            store=store,
            download_fn=lambda fid: "/tmp/x.pdf",
            upload_fn=lambda *a, **kw: None,
            say_fn=lambda **kw: posted.append(kw),
            pipeline_fn=lambda paths, client: _make_batch_result(n_ok=1),
        )

        assert posted, "Expected a result card to be posted"
        blocks = posted[0]["blocks"]
        context_texts = []
        for b in blocks:
            if b.get("type") == "context":
                for el in b.get("elements", []):
                    context_texts.append(el.get("text", ""))
        combined = " ".join(context_texts)
        assert "No COA" in combined or "no COA" in combined or "COA" in combined


# --------------------------------------------------------------------------- #
# Test: download failure for one file — others still processed
# --------------------------------------------------------------------------- #

class TestDownloadFailure:

    def test_failed_file_in_errors_others_processed(self):
        client = _make_client()
        store = _make_store(client)
        pipeline_paths: list[list] = []

        def _download(fid: str) -> str:
            if fid == "F-BAD":
                raise IOError("connection refused")
            return f"/tmp/{fid}.pdf"

        def _pipeline(paths, client):
            pipeline_paths.append(list(paths))
            return BatchResult(workbooks={}, docs=[], errors=[])

        outcome = process_shared_files(
            channel_id="C-TEST",
            file_ids=["F-OK", "F-BAD"],
            store=store,
            download_fn=_download,
            upload_fn=lambda *a, **kw: None,
            say_fn=lambda **kw: None,
            pipeline_fn=_pipeline,
        )

        assert any("F-BAD" in e for e in outcome.errors)
        # pipeline received only the successful download
        assert pipeline_paths
        assert all("F-BAD" not in p for p in pipeline_paths[0])


# --------------------------------------------------------------------------- #
# Test: handle_file_share guard — bot_id set → worker NOT called
# --------------------------------------------------------------------------- #

class TestHandleFileShareGuards:

    def _run_handle(self, event: dict, worker_calls: list) -> None:
        from app.slack_app import handle_file_share

        store = _make_store(_make_client())

        with patch("app.slack_app.run_share") as mock_run:
            mock_run.side_effect = lambda **kw: worker_calls.append(kw)
            handle_file_share(event, client=MagicMock(), store=store)
            # Capture submit calls on the executor instead
        # We monkeypatch run_share; but handle_file_share submits to _executor.
        # Patch _executor.submit to intercept without spawning threads.

    def test_bot_message_ignored(self):
        from app.slack_app import handle_file_share
        calls: list = []
        with patch("app.slack_app._executor") as mock_exec:
            mock_exec.submit.side_effect = lambda fn, **kw: calls.append(kw)
            event = {"bot_id": "B-BOT", "channel": "C-TEST", "files": [{"id": "F1"}]}
            handle_file_share(event, client=MagicMock(), store=_make_store(_make_client()))
        assert calls == []

    def test_no_files_ignored(self):
        from app.slack_app import handle_file_share
        calls: list = []
        with patch("app.slack_app._executor") as mock_exec:
            mock_exec.submit.side_effect = lambda fn, **kw: calls.append(kw)
            event = {"channel": "C-TEST", "files": [], "subtype": None}
            handle_file_share(event, client=MagicMock(), store=_make_store(_make_client()))
        assert calls == []

    def test_irrelevant_subtype_ignored(self):
        from app.slack_app import handle_file_share
        calls: list = []
        with patch("app.slack_app._executor") as mock_exec:
            mock_exec.submit.side_effect = lambda fn, **kw: calls.append(kw)
            event = {"channel": "C-TEST", "files": [{"id": "F1"}], "subtype": "bot_message"}
            handle_file_share(event, client=MagicMock(), store=_make_store(_make_client()))
        assert calls == []

    def test_valid_file_share_launches_worker(self):
        from app.slack_app import handle_file_share
        calls: list = []
        with patch("app.slack_app._executor") as mock_exec:
            mock_exec.submit.side_effect = lambda fn, **kw: calls.append(kw)
            event = {"channel": "C-TEST", "files": [{"id": "F1"}, {"id": "F2"}]}
            handle_file_share(event, client=MagicMock(), store=_make_store(_make_client()))
        assert len(calls) == 1
        assert calls[0]["channel_id"] == "C-TEST"
        assert calls[0]["file_ids"] == ["F1", "F2"]


# --------------------------------------------------------------------------- #
# Test: Block Kit validity
# --------------------------------------------------------------------------- #

class TestBlockBuilders:

    def test_needs_setup_blocks_is_list(self):
        blocks = needs_setup_blocks()
        assert isinstance(blocks, list)

    def test_needs_setup_blocks_non_empty(self):
        blocks = needs_setup_blocks()
        assert len(blocks) > 0

    def test_needs_setup_blocks_all_dicts(self):
        blocks = needs_setup_blocks()
        assert all(isinstance(b, dict) for b in blocks)

    def test_needs_setup_has_section_and_actions(self):
        types = {b["type"] for b in needs_setup_blocks()}
        assert "section" in types
        assert "actions" in types

    def test_result_card_is_list(self):
        blocks = result_card(n_files=2, n_processed=2, workbooks=["L.xlsx"], errors=[])
        assert isinstance(blocks, list)

    def test_result_card_all_dicts(self):
        blocks = result_card(n_files=2, n_processed=2, workbooks=["L.xlsx"], errors=[])
        assert all(isinstance(b, dict) for b in blocks)

    def test_result_card_with_errors_has_error_block(self):
        blocks = result_card(
            n_files=3, n_processed=2, workbooks=[], errors=["file F-BAD: download failed — timeout"]
        )
        combined = str(blocks)
        assert "Errors" in combined or "error" in combined.lower()

    def test_result_card_coa_missing_has_context_block(self):
        blocks = result_card(n_files=1, n_processed=1, workbooks=["L.xlsx"], errors=[], coa_missing=True)
        types = [b["type"] for b in blocks]
        assert "context" in types

    def test_result_card_no_coa_missing_no_context_block(self):
        blocks = result_card(n_files=1, n_processed=1, workbooks=["L.xlsx"], errors=[], coa_missing=False)
        types = [b["type"] for b in blocks]
        assert "context" not in types

    def test_result_card_no_files_edge_case(self):
        blocks = result_card(n_files=0, n_processed=0, workbooks=[], errors=[])
        assert isinstance(blocks, list)
        assert len(blocks) > 0
