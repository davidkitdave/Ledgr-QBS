"""``SlackLedgerStore`` — the channel-hosted FY ledger workbook (Slack = file store).

Architecture
------------
The ADK graph is **Slack-agnostic**: nodes only prepare a serializable
``state["ledger_rows"]`` payload. This store — owned by the runner layer — is
the only place that talks to Slack about the ledger workbook. Each drop:

1. Look up the Firestore pointer ``clients/{client_id}/ledgers/{fy}`` to find the
   channel's current workbook ``slack_file_id`` (if any).
2. If a pointer exists, **download the current workbook bytes** from Slack (via
   the parked SSRF-hardened downloader) and open it; else start a **fresh
   workbook** with the right exporter's sheet layout.
3. **Append** the new rows into the correct sheet, **idempotently** — a stable
   per-document key recorded in a hidden trailing column dedupes re-processing so
   the same document never double-appends.
4. **Re-upload** the updated workbook via ``files_upload_v2`` and **update the
   Firestore pointer** with the new ``slack_file_id``.

Concurrency: two drops racing the same FY workbook are serialized **per channel**
by an in-process lock keyed on ``(channel_id, fy)``. (A multi-instance Cloud Run
deployment would back this with a Firestore transaction on the pointer doc; noted
for the deploy step.)

Both the Slack client and the Firestore client are **injectable** so the whole
store is unit-testable with fakes.
"""

from __future__ import annotations

import io
import logging
import threading
import urllib.parse
import urllib.request
from typing import Any, Optional

from openpyxl import Workbook, load_workbook

from invoice_processing.export.exporters import (
    BankStatementExporter,
    get_exporter,
)

logger = logging.getLogger(__name__)

#: Firestore collection holding client profiles (pointer lives in a subcollection).
_CLIENTS_COLLECTION = "clients"
#: Subcollection name for the per-FY ledger pointer docs.
_LEDGERS_SUBCOLLECTION = "ledgers"

#: Hidden trailing column header carrying the idempotency doc key on every row.
#: Re-processing a document re-emits the same key; we skip rows whose key is
#: already present in the sheet, so a re-drop never double-appends.
DEDUPE_COL = "_ledgr_doc_key"

#: Sheet titles for the invoice ledger workbook (mirrors LedgerExporter).
_INVOICE_SHEETS = ("Purchase", "Sales")


def _is_slack_host(host: str) -> bool:
    host = (host or "").lower()
    return host == "slack.com" or host.endswith(".slack.com")


class SlackLedgerStore:
    """Fetch → append → re-upload the channel's FY ledger workbook.

    Args:
        db: A Firestore client (or compatible fake) holding the ledger pointer.
        opener: Optional ``urllib`` opener used to stream workbook bytes from
            Slack; defaults to a plain opener. Injected in tests is unnecessary
            because the fake Slack client returns bytes directly (see below).
    """

    def __init__(self, db: Any, *, opener: Optional[Any] = None) -> None:
        self._db = db
        self._opener = opener or urllib.request.build_opener()
        # Per-(channel, fy) locks serialize racing drops on the same workbook.
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------ #
    # Firestore pointer
    # ------------------------------------------------------------------ #

    def _pointer_ref(self, client_id: str, fy: str) -> Any:
        return (
            self._db.collection(_CLIENTS_COLLECTION)
            .document(client_id)
            .collection(_LEDGERS_SUBCOLLECTION)
            .document(str(fy))
        )

    def get_pointer(self, client_id: str, fy: str) -> Optional[dict]:
        """Return the ledger pointer doc for ``(client_id, fy)`` or ``None``."""
        snap = self._pointer_ref(client_id, fy).get()
        if not snap.exists:
            return None
        return snap.to_dict()

    def _set_pointer(self, client_id: str, fy: str, slack_file_id: str, **extra: Any) -> None:
        doc = {"slack_file_id": slack_file_id, "fy": str(fy), "client_id": client_id}
        doc.update(extra)
        self._pointer_ref(client_id, fy).set(doc, merge=True)

    # ------------------------------------------------------------------ #
    # Per-channel serialization
    # ------------------------------------------------------------------ #

    def _lock_for(self, channel_id: str, fy: str) -> threading.Lock:
        key = (channel_id, str(fy))
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    # ------------------------------------------------------------------ #
    # Slack workbook download
    # ------------------------------------------------------------------ #

    def _download_workbook(self, slack_client: Any, slack_file_id: str) -> bytes:
        """Download a workbook's bytes from Slack by file id (SSRF-hardened).

        Mirrors the parked ``app.slack_app.slack_download_file`` host checks but
        returns bytes in-memory (no temp file) since workbooks are small.
        """
        info = slack_client.files_info(file=slack_file_id)
        file_meta = info["file"]
        url = file_meta.get("url_private_download") or file_meta.get("url_private")
        host = urllib.parse.urlparse(url).hostname or ""
        if not _is_slack_host(host):
            raise ValueError(f"refusing to download from non-slack host: {host!r}")
        token = getattr(slack_client, "token", None)
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}
        )
        with self._opener.open(req) as resp:
            return resp.read()

    # ------------------------------------------------------------------ #
    # Workbook helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _exporter_for(software: str):
        """Return the invoice exporter matching the client's accounting software."""
        return get_exporter(software or "qbs")

    def _fresh_invoice_workbook(self, software: str) -> Workbook:
        """Create an empty invoice workbook (Purchase + Sales sheets, headers only)."""
        exporter = self._exporter_for(software)
        wb = Workbook()
        for i, (title, cols) in enumerate(
            (("Purchase", exporter.purchase_cols), ("Sales", exporter.sales_cols))
        ):
            sheet = wb.active if i == 0 else wb.create_sheet(title)
            sheet.title = title
            sheet.append(list(cols) + [DEDUPE_COL])
        return wb

    def _fresh_bank_workbook(self) -> Workbook:
        """Create an empty bank workbook (header-only single placeholder sheet)."""
        wb = Workbook()
        sheet = wb.active
        sheet.title = "Bank"
        sheet.append(list(BankStatementExporter.BANK_COLS) + [DEDUPE_COL])
        return wb

    @staticmethod
    def _load_workbook(data: bytes) -> Workbook:
        return load_workbook(io.BytesIO(data))

    @staticmethod
    def _to_bytes(wb: Workbook) -> bytes:
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    @staticmethod
    def _existing_keys(sheet) -> set[str]:
        """Read the set of dedupe doc keys already present in a sheet.

        The dedupe key lives in the last column (header == :data:`DEDUPE_COL`).
        Sheets created before this column existed are treated as having no keys.
        """
        header = [c.value for c in sheet[1]] if sheet.max_row >= 1 else []
        if DEDUPE_COL not in header:
            return set()
        idx = header.index(DEDUPE_COL)
        keys: set[str] = set()
        for row in sheet.iter_rows(min_row=2, values_only=True):
            if idx < len(row) and row[idx]:
                keys.add(str(row[idx]))
        return keys

    @staticmethod
    def _append_rows(sheet, cols: list[str], rows: list[dict], doc_key: str, seen: set[str]) -> int:
        """Append ``rows`` to ``sheet`` in ``cols`` order + a trailing dedupe key.

        Skips entirely when ``doc_key`` is already present in ``seen``. Returns the
        number of rows appended (0 if deduped).
        """
        if doc_key in seen:
            return 0
        header = [c.value for c in sheet[1]] if sheet.max_row >= 1 else []
        # Ensure the dedupe column exists on legacy sheets.
        if DEDUPE_COL not in header:
            sheet.cell(row=1, column=len(header) + 1, value=DEDUPE_COL)
        for row in rows:
            sheet.append([row.get(c, "") for c in cols] + [doc_key])
        seen.add(doc_key)
        return len(rows)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def append_rows(
        self,
        *,
        client_id: str,
        fy: str,
        slack_client: Any,
        channel_id: str,
        batches: list[dict],
        software: str = "qbs",
        kind: str = "invoice",
    ) -> dict:
        """Append a run's rows to the channel FY workbook (fetch → append → upload).

        Args:
            client_id: The client whose ledger pointer is updated.
            fy: Financial-year label (the workbook is per-FY).
            slack_client: A Slack WebClient (or fake) for download + upload.
            channel_id: The channel the workbook is uploaded to.
            batches: A list of ``{"sheet": str, "doc_key": str, "rows": [dict]}``
                payloads (as produced by ``consolidate_node`` into
                ``state["ledger_rows"]``). ``sheet`` is "Purchase"/"Sales" for the
                invoice workbook or the bank account label for the bank workbook.
            software: The client's accounting software ("qbs"/"xero") — selects the
                exporter column layout for a fresh invoice workbook.
            kind: "invoice" or "bank" — selects workbook layout + filename.

        Returns:
            ``{"slack_file_id", "appended", "deduped", "filename"}``.
        """
        lock = self._lock_for(channel_id, fy)
        with lock:
            return self._append_rows_locked(
                client_id=client_id,
                fy=fy,
                slack_client=slack_client,
                channel_id=channel_id,
                batches=batches,
                software=software,
                kind=kind,
            )

    def _append_rows_locked(
        self,
        *,
        client_id: str,
        fy: str,
        slack_client: Any,
        channel_id: str,
        batches: list[dict],
        software: str,
        kind: str,
    ) -> dict:
        pointer = self.get_pointer(client_id, fy)

        if pointer and pointer.get("slack_file_id"):
            data = self._download_workbook(slack_client, pointer["slack_file_id"])
            wb = self._load_workbook(data)
        elif kind == "bank":
            wb = self._fresh_bank_workbook()
        else:
            wb = self._fresh_invoice_workbook(software)

        if kind == "bank":
            filename = f"BankStatement_FY{fy}.xlsx"
            cols = list(BankStatementExporter.BANK_COLS)
        else:
            filename = f"Ledger_FY{fy}.xlsx"
            exporter = self._exporter_for(software)
            sheet_cols = {
                "Purchase": list(exporter.purchase_cols),
                "Sales": list(exporter.sales_cols),
            }

        appended = 0
        deduped = 0
        for batch in batches:
            sheet_name = batch["sheet"]
            doc_key = str(batch["doc_key"])
            rows = batch.get("rows") or []

            sheet = self._get_or_create_sheet(wb, sheet_name, kind)
            if kind == "bank":
                cols_for_sheet = cols
            else:
                cols_for_sheet = sheet_cols.get(sheet_name, list(exporter.purchase_cols))

            seen = self._existing_keys(sheet)
            n = self._append_rows(sheet, cols_for_sheet, rows, doc_key, seen)
            if n == 0:
                deduped += 1
            else:
                appended += n

        new_bytes = self._to_bytes(wb)
        result = slack_client.files_upload_v2(
            channel=channel_id,
            filename=filename,
            file=new_bytes,
            title=filename,
        )
        new_file_id = self._extract_uploaded_file_id(result)
        if new_file_id:
            self._set_pointer(client_id, fy, new_file_id, channel_id=channel_id, kind=kind)

        return {
            "slack_file_id": new_file_id,
            "appended": appended,
            "deduped": deduped,
            "filename": filename,
        }

    @staticmethod
    def _get_or_create_sheet(wb: Workbook, sheet_name: str, kind: str):
        """Return the named sheet, creating it (with no header) if absent.

        For the bank workbook the first fresh sheet is titled "Bank"; the first
        real account batch renames it instead of leaving an empty placeholder.
        """
        if sheet_name in wb.sheetnames:
            return wb[sheet_name]
        # Reuse an empty default "Bank" placeholder for the first bank account.
        if kind == "bank" and "Bank" in wb.sheetnames and wb["Bank"].max_row <= 1:
            sheet = wb["Bank"]
            sheet.title = sheet_name
            return sheet
        return wb.create_sheet(sheet_name)

    # ------------------------------------------------------------------ #
    # Public read API
    # ------------------------------------------------------------------ #

    def read_rows(
        self,
        client_id: str,
        fy: str,
        slack_client: Any,
        channel_id: str,
    ) -> list[dict]:
        """Download the current FY workbook and return all data rows as dicts.

        Fetches the workbook pointed to by Firestore ``clients/{client_id}/ledgers/{fy}``.
        Strips the hidden ``_ledgr_doc_key`` (DEDUPE_COL) trailing column before
        returning so callers never see the internal deduplication key.

        Returns an empty list when no pointer exists yet (ledger not started).

        Args:
            client_id: The client whose ledger pointer to look up.
            fy: The financial-year label (e.g. ``"2026"``).
            slack_client: A Slack WebClient (or fake) used to download the file.
            channel_id: Unused here but kept for symmetry with ``append_rows``
                (e.g. for logging or future per-channel locking).

        Returns:
            A list of dicts keyed by sheet column headers, one entry per data
            row across ALL sheets in the workbook (Purchase + Sales for invoice
            workbooks; per-account sheets for bank workbooks).  Each dict also
            carries ``"_sheet"`` so callers can distinguish row origin.
        """
        pointer = self.get_pointer(client_id, fy)
        if not pointer or not pointer.get("slack_file_id"):
            return []

        data = self._download_workbook(slack_client, pointer["slack_file_id"])
        wb = self._load_workbook(data)

        rows: list[dict] = []
        for sheet in wb.worksheets:
            if sheet.max_row < 1:
                continue
            raw_headers = [c.value for c in sheet[1]]
            # Find the DEDUPE_COL index to strip it from output.
            dedupe_idx: Optional[int] = None
            if DEDUPE_COL in raw_headers:
                dedupe_idx = raw_headers.index(DEDUPE_COL)
            headers = [h for h in raw_headers if h != DEDUPE_COL]

            for row in sheet.iter_rows(min_row=2, values_only=True):
                # Build the dict, skipping the dedupe column position.
                row_dict: dict = {"_sheet": sheet.title}
                for idx, header in enumerate(raw_headers):
                    if dedupe_idx is not None and idx == dedupe_idx:
                        continue
                    if header is not None:
                        row_dict[header] = row[idx] if idx < len(row) else None
                # Skip entirely-blank rows (all values None).
                if any(v is not None for k, v in row_dict.items() if k != "_sheet"):
                    rows.append(row_dict)

        return rows

    @staticmethod
    def _extract_uploaded_file_id(result: Any) -> Optional[str]:
        """Pull the uploaded file id out of a ``files_upload_v2`` response.

        ``files_upload_v2`` returns ``{"files": [{"id": ...}]}`` (a list) or the
        legacy ``{"file": {"id": ...}}`` shape; handle both + a plain dict.
        """
        if result is None:
            return None
        data = result.data if hasattr(result, "data") else result
        if not isinstance(data, dict):
            return None
        files = data.get("files")
        if isinstance(files, list) and files:
            first = files[0]
            # v2 nests as {"files": [{"file": {...}}]} in some SDK versions.
            if isinstance(first, dict):
                if "id" in first:
                    return first["id"]
                nested = first.get("file")
                if isinstance(nested, dict):
                    return nested.get("id")
        f = data.get("file")
        if isinstance(f, dict):
            return f.get("id")
        return None
