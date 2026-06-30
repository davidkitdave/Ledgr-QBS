"""Page-window chunking for very large PDFs (only the ``ValidationError`` fallback uses this).

The pre-chunker at ``should_chunk_pdf`` is intentionally narrow: it returns
True **only** when the inline PDF would exceed Google's documented 50 MB
inline limit, or when the caller passes an explicit opt-in via env. All
other PDFs go through one Gemini call, because:

- Gemini 2.5 supports up to 1000 pages / 50 MB inline per call natively.
- ``default_llm_config`` now pins ``max_output_tokens=65536`` so the full
  structured JSON fits even for a 35-page, 96-receipt, 852-line PDF.
- The Phase-0 control experiment on
  ``feat/minimal-extract-control-experiment`` proved that blind page-window
  chunking *caused* truncation (680 of 852 lines lost on multi-receipt) and
  fragmented clean single-doc PDFs (18-page Starhub bill -> 12 fake
  ``invoice`` docs). Keep ``iter_pdf_page_chunks`` / ``merge_chunk_bundles``
  because the ``ValidationError`` fallback in ``ledger_extract`` still uses
  them when the un-chunked call genuinely fails to validate.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ledger_extract import ExtractedDocumentBundle

CHUNK_PAGE_SIZE = int(os.environ.get("LEDGR_PDF_CHUNK_PAGES", "5"))

# Google documented inline limit for Gemini 2.5 (50 MB). PDFs beyond this
# genuinely cannot be sent inline; they must be chunked (or staged via the
# Files API — out of scope here).
INLINE_PDF_BYTE_LIMIT = 50 * 1024 * 1024
LARGE_PDF_BYTE_THRESHOLD = int(
    os.environ.get("LEDGR_LARGE_PDF_BYTES", str(INLINE_PDF_BYTE_LIMIT))
)


def should_chunk_pdf(
    data: bytes,
    mime_type: str,
    *,
    page_count: int | None = None,
) -> bool:
    """Return True only when the PDF is too big to send inline in one call.

    Page count is intentionally NOT considered — it was the source of the
    Starhub / multi-receipt noise. The Phase-0 spike on a fresh
    ``feat/minimal-extract-control-experiment`` branch proved one
    ``generate_content`` call fits 35 pages / 19.4 MB / 852 lines without
    truncation when ``max_output_tokens=65536`` is set. Chunking was added
    in issue #16 to work around missing output budget, not because page
    count itself caused problems.

    The caller may opt in to a stricter byte guard via env
    (``LEDGR_LARGE_PDF_BYTES``); the default matches Google's 50 MB limit.
    """
    if mime_type != "application/pdf":
        return False
    return len(data) > LARGE_PDF_BYTE_THRESHOLD


def iter_pdf_page_chunks(
    data: bytes,
    *,
    chunk_size: int = CHUNK_PAGE_SIZE,
) -> list[tuple[bytes, int, int]]:
    """Return ``(chunk_bytes, start_page, end_page)`` tuples (1-based inclusive)."""
    import pypdfium2 as pdfium

    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")

    src = pdfium.PdfDocument(data)
    try:
        total_pages = len(src)
        if total_pages < 1:
            return [(data, 1, 1)]

        chunks: list[tuple[bytes, int, int]] = []
        for start in range(0, total_pages, chunk_size):
            end = min(start + chunk_size, total_pages) - 1
            page_indexes = list(range(start, end + 1))
            out = pdfium.PdfDocument.new()
            try:
                out.import_pages(src, page_indexes)
                fd, path = tempfile.mkstemp(suffix=".pdf")
                os.close(fd)
                try:
                    out.save(path)
                    chunk_bytes = Path(path).read_bytes()
                finally:
                    Path(path).unlink(missing_ok=True)
            finally:
                out.close()
            chunks.append((chunk_bytes, start + 1, end + 1))
        return chunks
    finally:
        src.close()


def _offset_page_range(page_range: list[int], chunk_start_page: int) -> list[int]:
    if not page_range:
        return page_range
    offset = chunk_start_page - 1
    return [int(page) + offset for page in page_range]


def merge_chunk_bundles(
    chunk_bundles: list[tuple[ExtractedDocumentBundle, int]],
) -> ExtractedDocumentBundle:
    """Merge per-chunk bundles, re-basing page numbers to the source PDF."""
    from .ledger_extract import ExtractedDocumentBundle

    documents = []
    skipped: set[int] = set()
    notes: list[str] = []

    for bundle, chunk_start in chunk_bundles:
        for doc in bundle.documents:
            adjusted = doc.model_copy(deep=True)
            adjusted.page_range = _offset_page_range(list(doc.page_range or []), chunk_start)
            documents.append(adjusted)
        for page in bundle.skipped_pages or []:
            skipped.add(int(page) + chunk_start - 1)
        if bundle.notes:
            notes.append(bundle.notes.strip())

    return ExtractedDocumentBundle(
        documents=documents,
        skipped_pages=sorted(skipped) if skipped else None,
        notes=" | ".join(notes) if notes else None,
    )


def extract_document_ledger_chunked(
    data: bytes,
    mime_type: str,
    *,
    extract_fn,
    chunk_size: int = CHUNK_PAGE_SIZE,
    **kwargs,
) -> ExtractedDocumentBundle:
    """Extract a large PDF via page windows and merge the faithful bundles."""
    chunk_bundles: list[tuple[ExtractedDocumentBundle, int]] = []
    for chunk_bytes, start_page, _end_page in iter_pdf_page_chunks(data, chunk_size=chunk_size):
        bundle = extract_fn(chunk_bytes, mime_type, **kwargs)
        chunk_bundles.append((bundle, start_page))
    return merge_chunk_bundles(chunk_bundles)
