"""PDF page counting for ledgr_agent credit estimates."""

from __future__ import annotations

import io


def count_input_pages(data: bytes, mime_type: str) -> int:
    """Return page count for an upload (PDF via pdfplumber, else 1)."""
    if mime_type != "application/pdf":
        return 1
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return len(pdf.pages) or 1
    except Exception:
        return 1
