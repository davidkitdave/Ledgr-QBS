"""MIME type helpers for ledgr_agent (no invoice_processing imports)."""

from __future__ import annotations

from pathlib import Path

_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tiff": "image/tiff",
}


def mime_for(path: str | Path) -> str:
    """Return MIME type for a file path based on suffix."""
    return _MIME_BY_EXT.get(Path(path).suffix.lower(), "application/octet-stream")
