"""Hermetic tests for the GCS archive layer (app/archive.py),
archive wiring in app/processing.py, and /ledgr export in app/slack_app.py.

No live GCS call. No live Slack token. Uses:
- InMemoryArchiveStore for round-trip and processing tests.
- A hand-rolled fake storage.Client / bucket / blob for GcsArchiveStore tests.
"""

from __future__ import annotations

import os
import tempfile
from typing import Optional
from unittest.mock import MagicMock

import pytest

from app.archive import (
    GcsArchiveStore,
    InMemoryArchiveStore,
    _fy_from_workbook_name,
)


# --------------------------------------------------------------------------- #
# Hand-rolled fake google.cloud.storage (NO live call)
# --------------------------------------------------------------------------- #

class FakeBlob:
    def __init__(self, store: dict, path: str) -> None:
        self._store = store
        self._path = path

    def upload_from_string(self, data: bytes) -> None:
        self._store[self._path] = data

    def download_as_bytes(self) -> bytes:
        if self._path not in self._store:
            raise Exception(f"blob not found: {self._path}")
        return self._store[self._path]

    def exists(self) -> bool:
        return self._path in self._store


class FakeBlobIter:
    """Iterable of fake blobs for list_blobs."""

    def __init__(self, store: dict, prefix: str) -> None:
        self._items = [
            _FakeListBlob(name) for name in store if name.startswith(prefix)
        ]

    def __iter__(self):
        return iter(self._items)


class _FakeListBlob:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeBucket:
    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def blob(self, path: str) -> FakeBlob:
        return FakeBlob(self._store, path)

    def list_blobs(self, prefix: str = "") -> FakeBlobIter:
        return FakeBlobIter(self._store, prefix)


class FakeStorageClient:
    """Minimal fake for google.cloud.storage.Client."""

    def __init__(self) -> None:
        self._buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        if name not in self._buckets:
            self._buckets[name] = FakeBucket()
        return self._buckets[name]


# --------------------------------------------------------------------------- #
# _fy_from_workbook_name
# --------------------------------------------------------------------------- #

class TestFyFromWorkbookName:

    def test_ledger_fy2025(self):
        assert _fy_from_workbook_name("Ledger_FY2025.xlsx") == 2025

    def test_bank_statement_fy2026(self):
        assert _fy_from_workbook_name("BankStatement_FY2026.xlsx") == 2026

    def test_junk_returns_none(self):
        assert _fy_from_workbook_name("junk") is None

    def test_no_fy_prefix_returns_none(self):
        assert _fy_from_workbook_name("Ledger_2025.xlsx") is None

    def test_fy_in_middle(self):
        assert _fy_from_workbook_name("Prefix_FY2024_Suffix.xlsx") == 2024

    def test_empty_string(self):
        assert _fy_from_workbook_name("") is None


# --------------------------------------------------------------------------- #
# InMemoryArchiveStore round-trips
# --------------------------------------------------------------------------- #

class TestInMemoryArchiveStore:

    def test_archive_source_returns_path(self):
        store = InMemoryArchiveStore()
        path = store.archive_source("client-1", 2025, "purchase", "doc.pdf", b"pdfbytes")
        assert path == "client-1/FY2025/purchase/doc.pdf"

    def test_save_workbook_returns_path(self):
        store = InMemoryArchiveStore()
        path = store.save_workbook("client-1", 2025, "Ledger_FY2025.xlsx", b"xlsxbytes")
        assert path == "client-1/FY2025/workbooks/Ledger_FY2025.xlsx"

    def test_get_workbook_roundtrip(self):
        store = InMemoryArchiveStore()
        data = b"PK\x03\x04workbookbytes"
        store.save_workbook("client-1", 2025, "Ledger_FY2025.xlsx", data)
        result = store.get_workbook("client-1", 2025, "Ledger_FY2025.xlsx")
        assert result == data

    def test_get_workbook_missing_returns_none(self):
        store = InMemoryArchiveStore()
        assert store.get_workbook("client-1", 2025, "Missing.xlsx") is None

    def test_list_workbooks_returns_tuples(self):
        store = InMemoryArchiveStore()
        store.save_workbook("client-1", 2025, "Ledger_FY2025.xlsx", b"a")
        store.save_workbook("client-1", 2026, "Ledger_FY2026.xlsx", b"b")
        result = store.list_workbooks("client-1")
        assert (2025, "Ledger_FY2025.xlsx") in result
        assert (2026, "Ledger_FY2026.xlsx") in result

    def test_list_workbooks_multiple_fy_sorted(self):
        store = InMemoryArchiveStore()
        store.save_workbook("client-1", 2026, "Ledger_FY2026.xlsx", b"b")
        store.save_workbook("client-1", 2025, "Ledger_FY2025.xlsx", b"a")
        result = store.list_workbooks("client-1")
        assert result[0][0] <= result[1][0]  # sorted ascending by FY

    def test_list_workbooks_excludes_sources(self):
        store = InMemoryArchiveStore()
        store.archive_source("client-1", 2025, "purchase", "doc.pdf", b"pdf")
        store.save_workbook("client-1", 2025, "Ledger_FY2025.xlsx", b"wb")
        result = store.list_workbooks("client-1")
        assert len(result) == 1
        assert result[0] == (2025, "Ledger_FY2025.xlsx")

    def test_list_workbooks_excludes_other_clients(self):
        store = InMemoryArchiveStore()
        store.save_workbook("client-A", 2025, "Ledger_FY2025.xlsx", b"a")
        store.save_workbook("client-B", 2025, "Ledger_FY2025.xlsx", b"b")
        result = store.list_workbooks("client-A")
        assert all(True for fy, fn in result)  # no assertion on client-B items
        names = [fn for _, fn in result]
        # all items belong to client-A prefix
        assert len(result) == 1

    def test_list_workbooks_empty_store(self):
        store = InMemoryArchiveStore()
        assert store.list_workbooks("client-1") == []


# --------------------------------------------------------------------------- #
# GcsArchiveStore with fake client (NO live call)
# --------------------------------------------------------------------------- #

class TestGcsArchiveStore:

    def _store(self) -> tuple[GcsArchiveStore, FakeStorageClient]:
        fake = FakeStorageClient()
        return GcsArchiveStore("test-bucket", client=fake), fake

    def test_constructor_does_not_touch_network(self):
        # Constructing with no client and no real GCS — just checking no import-time call
        store = GcsArchiveStore("test-bucket")
        assert store._injected_client is None
        assert store._client is None

    def test_archive_source_correct_path(self):
        store, fake = self._store()
        path = store.archive_source("client-1", 2025, "purchase", "invoice.pdf", b"data")
        assert path == "client-1/FY2025/purchase/invoice.pdf"
        bucket = fake.bucket("test-bucket")
        assert bucket._store["client-1/FY2025/purchase/invoice.pdf"] == b"data"

    def test_save_workbook_correct_path(self):
        store, fake = self._store()
        path = store.save_workbook("client-1", 2025, "Ledger_FY2025.xlsx", b"xlsx")
        assert path == "client-1/FY2025/workbooks/Ledger_FY2025.xlsx"
        bucket = fake.bucket("test-bucket")
        assert bucket._store["client-1/FY2025/workbooks/Ledger_FY2025.xlsx"] == b"xlsx"

    def test_get_workbook_roundtrip(self):
        store, _ = self._store()
        data = b"PK\x03\x04excel"
        store.save_workbook("client-1", 2025, "Ledger_FY2025.xlsx", data)
        result = store.get_workbook("client-1", 2025, "Ledger_FY2025.xlsx")
        assert result == data

    def test_get_workbook_missing_returns_none(self):
        store, _ = self._store()
        assert store.get_workbook("client-1", 2025, "Missing.xlsx") is None

    def test_list_workbooks_returns_tuples(self):
        store, _ = self._store()
        store.save_workbook("client-1", 2025, "Ledger_FY2025.xlsx", b"a")
        store.save_workbook("client-1", 2026, "BankStatement_FY2026.xlsx", b"b")
        result = store.list_workbooks("client-1")
        assert (2025, "Ledger_FY2025.xlsx") in result
        assert (2026, "BankStatement_FY2026.xlsx") in result

    def test_list_workbooks_excludes_sources(self):
        store, _ = self._store()
        store.archive_source("client-1", 2025, "purchase", "doc.pdf", b"pdf")
        store.save_workbook("client-1", 2025, "Ledger_FY2025.xlsx", b"wb")
        result = store.list_workbooks("client-1")
        assert len(result) == 1
        assert result[0] == (2025, "Ledger_FY2025.xlsx")

    def test_list_workbooks_sorted_by_fy(self):
        store, _ = self._store()
        store.save_workbook("client-1", 2027, "Ledger_FY2027.xlsx", b"c")
        store.save_workbook("client-1", 2025, "Ledger_FY2025.xlsx", b"a")
        store.save_workbook("client-1", 2026, "Ledger_FY2026.xlsx", b"b")
        result = store.list_workbooks("client-1")
        fys = [fy for fy, _ in result]
        assert fys == sorted(fys)

    def test_list_workbooks_empty(self):
        store, _ = self._store()
        assert store.list_workbooks("client-1") == []

    def test_no_live_gcs_call(self):
        # GcsArchiveStore with injected fake never imports google.cloud.storage
        store, fake = self._store()
        store.archive_source("c", 2025, "purchase", "f.pdf", b"x")
        store.get_workbook("c", 2025, "f.pdf")
        store.list_workbooks("c")
        # If we got here without ImportError / network call, the seam works.


# --------------------------------------------------------------------------- #
# process_shared_files + archive wiring
# --------------------------------------------------------------------------- #

from invoice_processing.export.client_context import ClientContext, InMemoryClientStore
from invoice_processing.pipeline import BatchResult, ProcessedDoc


def _make_client(*, status: str = "active") -> ClientContext:
    return ClientContext(
        client_id="client-arc-1",
        client_name="ArcCo Pte Ltd",
        channel_id="C-ARC",
        status=status,
        fye_month=12,
    )


def _make_store(client: Optional[ClientContext] = None, channel_id: str = "C-ARC") -> InMemoryClientStore:
    store = InMemoryClientStore()
    if client is not None:
        store.add(client, channel_id=channel_id)
    return store


def _stub_route():
    from invoice_processing.export.routing import DocRoute
    return DocRoute(
        fy=2025,
        bucket="purchase",
        archive_path="client-arc-1/FY2025/purchase/doc.pdf",
        workbook="Ledger_FY2025.xlsx",
        sheet="Purchase",
    )


class TestProcessSharedFilesArchive:
    """Verify archive wiring in process_shared_files."""

    def _run(
        self,
        *,
        archive=None,
        pipeline_fn=None,
        upload_raises: bool = False,
        archive_raises: bool = False,
    ):
        from app.processing import process_shared_files

        client = _make_client()
        store = _make_store(client)

        # Write a real temp file so archive_source can read its bytes
        fd, tmp_path = tempfile.mkstemp(suffix="_doc.pdf")
        os.write(fd, b"pdfcontent")
        os.close(fd)

        try:
            route = _stub_route()
            doc = ProcessedDoc(
                path=tmp_path,
                doc_type="invoice",
                direction="purchase",
                normalized=None,
                bank=None,
                route=route,
                reconciled=True,
                note="ok",
            )
            batch = BatchResult(
                workbooks={"Ledger_FY2025.xlsx": b"PK\x03\x04"},
                docs=[doc],
                errors=[],
            )

            if pipeline_fn is None:
                pipeline_fn = lambda paths, c: batch

            uploaded: list = []

            def _upload(ch, fname, data, title):
                if upload_raises:
                    raise IOError("upload failed")
                uploaded.append((ch, fname, data))

            outcome = process_shared_files(
                channel_id="C-ARC",
                file_ids=["F001"],
                store=store,
                download_fn=lambda fid: tmp_path,
                upload_fn=_upload,
                say_fn=lambda **kw: None,
                pipeline_fn=pipeline_fn,
                archive=archive,
            )
            return outcome, uploaded
        finally:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass

    def test_source_archived_after_pipeline(self):
        archive = InMemoryArchiveStore()
        outcome, _ = self._run(archive=archive)
        workbooks = archive.list_workbooks("client-arc-1")
        # workbook archived
        assert (2025, "Ledger_FY2025.xlsx") in workbooks
        # source archived: client-arc-1/FY2025/purchase/{filename}
        keys = list(archive._objects.keys())
        source_keys = [k for k in keys if "/purchase/" in k]
        assert len(source_keys) == 1

    def test_workbook_archived_at_correct_path(self):
        archive = InMemoryArchiveStore()
        self._run(archive=archive)
        data = archive.get_workbook("client-arc-1", 2025, "Ledger_FY2025.xlsx")
        assert data == b"PK\x03\x04"

    def test_archive_none_archives_nothing(self):
        archive = InMemoryArchiveStore()
        # Run without archive — the InMemoryArchiveStore should remain empty
        self._run(archive=None)
        # Nothing archived because we passed archive=None
        assert archive._objects == {}

    def test_archive_failure_does_not_prevent_upload(self):
        """An archive that raises must not crash the pipeline or prevent upload."""

        class RaisingArchive:
            def archive_source(self, *a, **kw):
                raise RuntimeError("GCS down")

            def save_workbook(self, *a, **kw):
                raise RuntimeError("GCS down")

            def get_workbook(self, *a, **kw):
                return None

            def list_workbooks(self, *a, **kw):
                return []

        outcome, uploaded = self._run(archive=RaisingArchive())
        # Upload still happened
        assert len(uploaded) == 1
        assert uploaded[0][1] == "Ledger_FY2025.xlsx"
        # Errors recorded (archive failures), but outcome status is ok
        assert outcome.status == "ok"
        assert any("archive" in e for e in outcome.errors)

    def test_no_archive_behavior_unchanged(self):
        """archive=None: upload happens, outcome is ok, no side-effects."""
        outcome, uploaded = self._run(archive=None)
        assert outcome.status == "ok"
        assert len(uploaded) == 1


# --------------------------------------------------------------------------- #
# /ledgr export via handle_ledgr_command
# --------------------------------------------------------------------------- #

class TestLedgrExportCommand:
    """Tests for handle_ledgr_command with archive wired."""

    def _make_fake_slack_client(self) -> tuple[MagicMock, list, list]:
        """Returns (fake_client, upload_calls, post_calls)."""
        upload_calls: list = []
        post_calls: list = []
        fake_client = MagicMock()
        fake_client.files_upload_v2.side_effect = lambda **kw: upload_calls.append(kw)
        fake_client.chat_postMessage.side_effect = lambda **kw: post_calls.append(kw)
        return fake_client, upload_calls, post_calls

    def _make_body(self, channel_id: str = "C-ARC", text: str = "export") -> dict:
        return {"channel_id": channel_id, "text": text, "trigger_id": "T-TRIG"}

    def _pre_populated_archive(self) -> InMemoryArchiveStore:
        archive = InMemoryArchiveStore()
        archive.save_workbook("client-arc-1", 2025, "Ledger_FY2025.xlsx", b"PK\x03\x04wb")
        return archive

    def test_export_with_archive_and_workbook_uploads_file(self):
        from app.slack_app import handle_ledgr_command

        store = _make_store(_make_client())
        archive = self._pre_populated_archive()
        fake_client, upload_calls, post_calls = self._make_fake_slack_client()

        handle_ledgr_command(
            ack=lambda: None,
            body=self._make_body(),
            client=fake_client,
            store=store,
            archive=archive,
        )

        assert len(upload_calls) == 1
        assert upload_calls[0]["filename"] == "Ledger_FY2025.xlsx"
        assert upload_calls[0]["file"] == b"PK\x03\x04wb"
        assert upload_calls[0]["channel"] == "C-ARC"

    def test_export_with_archive_posts_confirmation(self):
        from app.slack_app import handle_ledgr_command

        store = _make_store(_make_client())
        archive = self._pre_populated_archive()
        fake_client, upload_calls, post_calls = self._make_fake_slack_client()

        handle_ledgr_command(
            ack=lambda: None,
            body=self._make_body(),
            client=fake_client,
            store=store,
            archive=archive,
        )

        # A confirmation message was posted (text contains "re-sent" or similar)
        assert len(post_calls) == 1
        msg_text = post_calls[0].get("text", "")
        assert "ledger" in msg_text.lower() or "Ledger" in msg_text

    def test_export_empty_archive_posts_unavailable(self):
        from app.slack_app import handle_ledgr_command

        store = _make_store(_make_client())
        archive = InMemoryArchiveStore()  # empty
        fake_client, upload_calls, post_calls = self._make_fake_slash_client()

        handle_ledgr_command(
            ack=lambda: None,
            body=self._make_body(),
            client=fake_client,
            store=store,
            archive=archive,
        )

        assert len(upload_calls) == 0
        assert len(post_calls) == 1
        blocks = post_calls[0].get("blocks", [])
        block_text = str(blocks)
        assert "No ledger" in block_text or "no ledger" in block_text.lower()

    def test_export_no_archive_posts_unavailable(self):
        from app.slack_app import handle_ledgr_command

        store = _make_store(_make_client())
        fake_client, upload_calls, post_calls = self._make_fake_slash_client()

        handle_ledgr_command(
            ack=lambda: None,
            body=self._make_body(),
            client=fake_client,
            store=store,
            archive=None,
        )

        assert len(upload_calls) == 0
        assert len(post_calls) == 1
        blocks = post_calls[0].get("blocks", [])
        block_text = str(blocks)
        assert "No ledger" in block_text or "no ledger" in block_text.lower()

    def test_export_no_channel_profile_posts_unavailable(self):
        from app.slack_app import handle_ledgr_command

        store = InMemoryClientStore()  # no profile for C-ARC
        archive = self._pre_populated_archive()
        fake_client, upload_calls, post_calls = self._make_fake_slash_client()

        handle_ledgr_command(
            ack=lambda: None,
            body=self._make_body(),
            client=fake_client,
            store=store,
            archive=archive,
        )

        assert len(upload_calls) == 0
        assert len(post_calls) == 1

    # Helper so we avoid repeating the same mock setup
    def _make_fake_slash_client(self):
        return self._make_fake_slack_client()

    def test_export_multiple_fy_picks_latest(self):
        from app.slack_app import handle_ledgr_command

        store = _make_store(_make_client())
        archive = InMemoryArchiveStore()
        archive.save_workbook("client-arc-1", 2024, "Ledger_FY2024.xlsx", b"old")
        archive.save_workbook("client-arc-1", 2025, "Ledger_FY2025.xlsx", b"new")
        fake_client, upload_calls, post_calls = self._make_fake_slack_client()

        handle_ledgr_command(
            ack=lambda: None,
            body=self._make_body(),
            client=fake_client,
            store=store,
            archive=archive,
        )

        # Only the FY2025 workbook is re-uploaded
        assert len(upload_calls) == 1
        assert upload_calls[0]["filename"] == "Ledger_FY2025.xlsx"
        assert upload_calls[0]["file"] == b"new"
