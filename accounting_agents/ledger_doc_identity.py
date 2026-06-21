"""Per-document ledger dedupe identity (WS-5.4 / M3).

The research spec names ``file_id + page_range + reference``, but the Slack
``file_id`` rotates on every re-upload.  Keys therefore use ``sheet`` +
``reference`` + ``page_range`` only so re-dropping the same PDF stays
idempotent (ADR-0010).  ``page_range`` disambiguates N documents from one
multi-invoice PDF when references would otherwise collide.
"""

from __future__ import annotations


def ledger_doc_identity(
    sheet: str,
    reference: str | None,
    page_range: tuple[int, int] | None = None,
    *,
    index: int = 0,
) -> str:
    """Return the Firestore ``seen_doc_keys`` entry for one ledger document."""
    ref = (reference or "").strip() or f"i{index}"
    if page_range is not None:
        start, end = page_range
        return f"{sheet}:{ref}:{start}-{end}"
    return f"{sheet}:{ref}"
