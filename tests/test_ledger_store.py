"""Hermetic tests for ``SlackLedgerStore`` — fetch → append → re-upload.

Mocks Slack (an in-memory file store keyed by id, with ``files_upload_v2`` /
``files_info`` + an opener that serves the stored bytes) and Firestore (the
shared ``tests._fake_firestore.FakeFirestore``). No network, no real Slack.

Proves:
- A first append starts a fresh workbook and writes rows into the right sheet.
- A second append fetches the current workbook (via the pointer) and adds the new
  batch — both batches end up in the workbook.
- Re-processing the SAME document (same dedupe doc_key) does NOT double-append.
- The Firestore pointer is updated to the latest uploaded file id.
"""

from __future__ import annotations

import io
import uuid

from openpyxl import load_workbook

from accounting_agents.ledger_store import DEDUPE_COL, SlackLedgerStore
from tests._fake_firestore import FakeFirestore


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Serves bytes for a previously-recorded https URL → bytes mapping."""

    def __init__(self, urls: dict[str, bytes]):
        self._urls = urls

    def open(self, req):
        url = req.full_url if hasattr(req, "full_url") else req
        return _FakeResponse(self._urls[url])


class FakeSlackClient:
    """In-memory Slack file store supporting upload + info + download."""

    token = "xoxb-test"

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.urls: dict[str, bytes] = {}
        self.uploads: list[dict] = []
        self._posts: list[dict] = []
        self.updates: list[dict] = []

    def chat_postMessage(self, **kwargs):
        self._posts.append(kwargs)
        return {"ok": True, "ts": f"{len(self._posts)}.000"}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}

    def files_upload_v2(self, *, channel, filename, file, title=None):
        file_id = "F" + uuid.uuid4().hex[:10]
        self.files[file_id] = file
        url = f"https://files.slack.com/{file_id}/{filename}"
        self.urls[url] = file
        self.uploads.append({"channel": channel, "filename": filename, "id": file_id})
        return {"files": [{"id": file_id, "url_private_download": url}]}

    def files_info(self, *, file):
        # Find the URL that maps to this file id's bytes.
        data = self.files[file]
        url = next(u for u, b in self.urls.items() if b is data)
        return {"file": {"id": file, "url_private_download": url, "name": "ledger.xlsx"}}

    def opener(self) -> _FakeOpener:
        return _FakeOpener(self.urls)


def _row(desc: str) -> dict:
    return {
        "Invoice Number": "INV-1",
        "Description": desc,
        "Source Amount": 100.0,
        "Account Code / COA": "6000",
    }


def _read_sheet_rows(data: bytes, sheet: str) -> list[tuple]:
    wb = load_workbook(io.BytesIO(data))
    ws = wb[sheet]
    return list(ws.iter_rows(min_row=2, values_only=True))


def _make_store(slack: FakeSlackClient) -> SlackLedgerStore:
    return SlackLedgerStore(FakeFirestore(), opener=slack.opener())


def test_first_append_starts_fresh_workbook():
    slack = FakeSlackClient()
    store = _make_store(slack)

    result = store.append_rows(
        client_id="c1",
        fy="2026",
        slack_client=slack,
        channel_id="C1",
        software="qbs",
        kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F1:Purchase:INV-1", "rows": [_row("first")]}],
    )

    assert result["appended"] == 1
    assert result["deduped"] == 0
    assert result["slack_file_id"]
    data = slack.files[result["slack_file_id"]]
    rows = _read_sheet_rows(data, "Purchase")
    assert len(rows) == 1
    # Pointer updated.
    ptr = store.get_pointer("c1", "2026")
    assert ptr["slack_file_id"] == result["slack_file_id"]


def test_second_append_fetches_and_adds_batch():
    slack = FakeSlackClient()
    store = _make_store(slack)

    store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F1:Purchase:INV-1", "rows": [_row("first")]}],
    )
    result2 = store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F2:Purchase:INV-2", "rows": [_row("second")]}],
    )

    assert result2["appended"] == 1
    data = slack.files[result2["slack_file_id"]]
    rows = _read_sheet_rows(data, "Purchase")
    # Both batches present in the workbook now.
    descriptions = [r for row in rows for r in row if r in ("first", "second")]
    assert "first" in descriptions and "second" in descriptions
    assert len(rows) == 2
    # Pointer advanced to the newest upload.
    assert store.get_pointer("c1", "2026")["slack_file_id"] == result2["slack_file_id"]


def test_reprocessing_same_doc_is_deduped():
    slack = FakeSlackClient()
    store = _make_store(slack)

    store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F1:Purchase:INV-1", "rows": [_row("first")]}],
    )
    # Re-process the SAME document (same doc_key) → no double-append.
    result2 = store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F1:Purchase:INV-1", "rows": [_row("first")]}],
    )

    assert result2["appended"] == 0
    assert result2["deduped"] == 1
    data = slack.files[result2["slack_file_id"]]
    rows = _read_sheet_rows(data, "Purchase")
    assert len(rows) == 1  # still exactly one row


def test_bank_workbook_one_sheet_per_account():
    slack = FakeSlackClient()
    store = _make_store(slack)

    result = store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        kind="bank",
        batches=[
            {"sheet": "OCBC - 5001", "doc_key": "F1:OCBC:1", "rows": [{"Date": "01/02/2026", "Description": "x", "Balance": 10.0}]},
            {"sheet": "DBS - 9002", "doc_key": "F1:DBS:1", "rows": [{"Date": "01/02/2026", "Description": "y", "Balance": 20.0}]},
        ],
    )

    data = slack.files[result["slack_file_id"]]
    wb = load_workbook(io.BytesIO(data))
    assert "OCBC - 5001" in wb.sheetnames
    assert "DBS - 9002" in wb.sheetnames
    # The dedupe column is present on the header.
    header = [c.value for c in wb["OCBC - 5001"][1]]
    assert DEDUPE_COL in header
