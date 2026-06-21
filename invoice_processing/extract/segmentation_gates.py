"""Deterministic G2 page-coverage gates for multi-document extraction (WS-2.3).

Union of all ``page_range`` values must equal the input page set (minus
``skipped_pages``), with no gaps or overlaps. Violations flag segmentation
as uncertain on the understand path.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ledger_extract import ExtractedDocumentBundle

SEGMENTATION_UNCERTAIN_PREFIX = "segmentation uncertain"


def validate_page_ranges(
    page_ranges: list[tuple[int, int]],
    *,
    total_pages: int,
    skipped_pages: list[int] | None = None,
) -> tuple[bool, str]:
    """Check page ranges cover every non-skipped page exactly once."""
    if total_pages < 1:
        return False, "total_pages must be >= 1"

    skip = set(skipped_pages or [])
    if any(p < 1 or p > total_pages for p in skip):
        return False, f"skipped_pages out of bounds 1..{total_pages}: {sorted(skip)}"

    covered: set[int] = set()
    for start, end in page_ranges:
        if start < 1 or end < start or end > total_pages:
            return False, f"invalid page_range ({start}, {end}) for total_pages={total_pages}"
        for page in range(start, end + 1):
            if page in skip:
                continue
            if page in covered:
                return False, f"page {page} covered by more than one document"
            covered.add(page)

    expected = {p for p in range(1, total_pages + 1) if p not in skip}
    missing = expected - covered
    if missing:
        return False, f"gaps on pages {sorted(missing)}"
    return True, "ok"


def validate_bundle_page_coverage(
    bundle: ExtractedDocumentBundle,
    *,
    total_pages: int,
) -> tuple[bool, str]:
    """Validate an extracted bundle against the input file page count."""
    from .ledger_extract import bundle_page_ranges

    page_ranges = bundle_page_ranges(bundle)
    if not page_ranges:
        return False, "no page_range on extracted documents"
    return validate_page_ranges(
        page_ranges,
        total_pages=total_pages,
        skipped_pages=bundle.skipped_pages,
    )


def count_input_pages(data: bytes, mime_type: str) -> int:
    """Return 1-based page count for an upload (PDF via pdfplumber, else 1)."""
    if mime_type == "application/pdf":
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return len(pdf.pages) or 1
    return 1


def segmentation_uncertain_note(detail: str) -> str:
    """Format the G2 flag note appended to reconcile_note."""
    return f"{SEGMENTATION_UNCERTAIN_PREFIX}: {detail}"


def apply_segmentation_uncertain_flag(
    normalized: list,
    detail: str,
) -> None:
    """Mark every normalized invoice unreconciled with a segmentation note."""
    note = segmentation_uncertain_note(detail)
    for inv in normalized:
        inv.reconciled = False
        if inv.reconcile_note:
            inv.reconcile_note = f"{note}; {inv.reconcile_note}"
        else:
            inv.reconcile_note = note
