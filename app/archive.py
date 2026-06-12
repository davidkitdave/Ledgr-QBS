"""GCS archive store for Ledgr source documents and workbooks (spec §4 write-side).

Object-path scheme:
  Sources:   {client_id}/FY{fy}/{bucket}/{filename}
             bucket is one of: purchase | sales | bank
             (mirrors DocRoute.archive_path)
  Workbooks: {client_id}/FY{fy}/workbooks/{filename}

``GcsArchiveStore`` accepts an injected ``client`` for hermetic testing — the
constructor never touches the network regardless of whether a client is injected.
``google.cloud.storage`` is imported lazily so importing this module in tests
never requires the GCS SDK to be importable.
"""

from __future__ import annotations

import re
from typing import Optional, Protocol


# --------------------------------------------------------------------------- #
# Protocol — the seam used by processing.py and slack_app.py
# --------------------------------------------------------------------------- #

class ArchiveStore(Protocol):
    def archive_source(
        self,
        client_id: str,
        fy: int,
        bucket: str,
        filename: str,
        data: bytes,
    ) -> str:
        """Store a source document and return its object path."""
        ...

    def save_workbook(
        self,
        client_id: str,
        fy: int,
        filename: str,
        data: bytes,
    ) -> str:
        """Store a workbook and return its object path."""
        ...

    def get_workbook(
        self,
        client_id: str,
        fy: int,
        filename: str,
    ) -> Optional[bytes]:
        """Return workbook bytes, or None if not found."""
        ...

    def list_workbooks(self, client_id: str) -> list[tuple[int, str]]:
        """Return [(fy, filename)] for all archived workbooks for this client."""
        ...


# --------------------------------------------------------------------------- #
# In-memory implementation — dict-backed; for tests and local dev
# --------------------------------------------------------------------------- #

class InMemoryArchiveStore:
    """Dict-backed archive store for tests and local development."""

    def __init__(self) -> None:
        # keyed by object path -> bytes
        self._objects: dict[str, bytes] = {}

    def _source_path(self, client_id: str, fy: int, bucket: str, filename: str) -> str:
        return f"{client_id}/FY{fy}/{bucket}/{filename}"

    def _workbook_path(self, client_id: str, fy: int, filename: str) -> str:
        return f"{client_id}/FY{fy}/workbooks/{filename}"

    def archive_source(
        self,
        client_id: str,
        fy: int,
        bucket: str,
        filename: str,
        data: bytes,
    ) -> str:
        path = self._source_path(client_id, fy, bucket, filename)
        self._objects[path] = data
        return path

    def save_workbook(
        self,
        client_id: str,
        fy: int,
        filename: str,
        data: bytes,
    ) -> str:
        path = self._workbook_path(client_id, fy, filename)
        self._objects[path] = data
        return path

    def get_workbook(
        self,
        client_id: str,
        fy: int,
        filename: str,
    ) -> Optional[bytes]:
        path = self._workbook_path(client_id, fy, filename)
        return self._objects.get(path)

    def list_workbooks(self, client_id: str) -> list[tuple[int, str]]:
        """Return [(fy, filename)] across all FYs, sorted by (fy, filename)."""
        prefix = f"{client_id}/"
        workbook_prefix = "/workbooks/"
        results: list[tuple[int, str]] = []
        for path in self._objects:
            if not path.startswith(prefix):
                continue
            if workbook_prefix not in path:
                continue
            # path: {client_id}/FY{fy}/workbooks/{filename}
            rest = path[len(prefix):]  # FY{fy}/workbooks/{filename}
            parts = rest.split("/")
            if len(parts) < 3:
                continue
            fy_segment = parts[0]  # FY2025
            if not fy_segment.startswith("FY"):
                continue
            try:
                fy = int(fy_segment[2:])
            except ValueError:
                continue
            filename = "/".join(parts[2:])
            results.append((fy, filename))
        results.sort()
        return results


# --------------------------------------------------------------------------- #
# GCS implementation — production
# --------------------------------------------------------------------------- #

class GcsArchiveStore:
    """GCS-backed archive store for production.

    ``client`` injection seam: pass a fake storage.Client for tests.
    When ``client`` is None, a real ``google.cloud.storage.Client`` is created
    lazily on first use — the constructor never touches the network.
    ``bucket_name`` is the GCS bucket name (not including gs:// prefix).
    """

    def __init__(self, bucket_name: str, client=None) -> None:
        self._bucket_name = bucket_name
        self._injected_client = client  # test seam
        self._client = None            # lazy real client

    def _storage_client(self):
        if self._injected_client is not None:
            return self._injected_client
        if self._client is None:
            from google.cloud import storage  # noqa: PLC0415  lazy import
            self._client = storage.Client()
        return self._client

    def _bucket(self):
        return self._storage_client().bucket(self._bucket_name)

    def _source_path(self, client_id: str, fy: int, bucket: str, filename: str) -> str:
        return f"{client_id}/FY{fy}/{bucket}/{filename}"

    def _workbook_path(self, client_id: str, fy: int, filename: str) -> str:
        return f"{client_id}/FY{fy}/workbooks/{filename}"

    def archive_source(
        self,
        client_id: str,
        fy: int,
        bucket: str,
        filename: str,
        data: bytes,
    ) -> str:
        path = self._source_path(client_id, fy, bucket, filename)
        self._bucket().blob(path).upload_from_string(data)
        return path

    def save_workbook(
        self,
        client_id: str,
        fy: int,
        filename: str,
        data: bytes,
    ) -> str:
        path = self._workbook_path(client_id, fy, filename)
        self._bucket().blob(path).upload_from_string(data)
        return path

    def get_workbook(
        self,
        client_id: str,
        fy: int,
        filename: str,
    ) -> Optional[bytes]:
        path = self._workbook_path(client_id, fy, filename)
        blob = self._bucket().blob(path)
        try:
            if not blob.exists():
                return None
            return blob.download_as_bytes()
        except Exception:  # noqa: BLE001
            return None

    def list_workbooks(self, client_id: str) -> list[tuple[int, str]]:
        """Return [(fy, filename)] for all workbooks archived under this client."""
        prefix = f"{client_id}/"
        bucket = self._bucket()
        results: list[tuple[int, str]] = []
        try:
            blobs = bucket.list_blobs(prefix=prefix)
            for blob in blobs:
                name: str = blob.name
                # Expect: {client_id}/FY{fy}/workbooks/{filename}
                rest = name[len(prefix):]
                parts = rest.split("/")
                if len(parts) < 3:
                    continue
                fy_segment = parts[0]
                if parts[1] != "workbooks":
                    continue
                if not fy_segment.startswith("FY"):
                    continue
                try:
                    fy = int(fy_segment[2:])
                except ValueError:
                    continue
                filename = "/".join(parts[2:])
                if filename:
                    results.append((fy, filename))
        except Exception:  # noqa: BLE001
            return []
        results.sort()
        return results


# --------------------------------------------------------------------------- #
# Helper: parse FY from workbook filename
# --------------------------------------------------------------------------- #

_FY_RE = re.compile(r"FY(\d{4})")


def _fy_from_workbook_name(name: str) -> Optional[int]:
    """Extract the FY integer from a workbook filename.

    Examples:
        "Ledger_FY2025.xlsx"       -> 2025
        "BankStatement_FY2026.xlsx" -> 2026
        "junk"                      -> None
    """
    m = _FY_RE.search(name)
    if m is None:
        return None
    return int(m.group(1))
