"""Split large PDFs into page windows for extraction (issue #16).

Gemini can truncate JSON when a single multi-receipt PDF spans many pages.
Chunking keeps each multimodal call within a safe output budget while
preserving 1-based page_range semantics on merge.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ledger_extract import ExtractedDocumentBundle

CHUNK_PAGE_SIZE = int(os.environ.get("LEDGR_PDF_CHUNK_PAGES", "5"))
LARGE_PDF_PAGE_THRESHOLD = int(os.environ.get("LEDGR_LARGE_PDF_PAGES", "10"))
LARGE_PDF_BYTE_THRESHOLD = int(
    os.environ.get("LEDGR_LARGE_PDF_BYTES", str(10 * 1024 * 1024))
)


def should_chunk_pdf(
    data: bytes,
    mime_type: str,
    *,
    page_count: int | None = None,
) -> bool:
    """Return True when a single-call extraction is likely to truncate."""
    if mime_type != "application/pdf":
        return False
    if len(data) > LARGE_PDF_BYTE_THRESHOLD:
        return True
    if page_count is None:
        from .segmentation_gates import count_input_pages

        page_count = count_input_pages(data, mime_type)
    return page_count > LARGE_PDF_PAGE_THRESHOLD


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
