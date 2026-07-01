"""ValidationError-only retry helpers for ``read_doc`` (no blind pre-chunking)."""

from __future__ import annotations

import io
import logging
from typing import Any, Callable

from google.genai import types
from pydantic import ValidationError

from ledgr_agent.internal.gemini import count_input_pages, default_llm_config
from ledgr_agent.internal.schemas import BUNDLE_READER_INSTRUCTION, READ_PROMPT, ReadDocumentBundle

_log = logging.getLogger(__name__)

ReadFn = Callable[[bytes, str, str], tuple[ReadDocumentBundle, dict[str, Any]]]


def _merge_bundles(primary: ReadDocumentBundle, secondary: ReadDocumentBundle) -> ReadDocumentBundle:
    """Merge two commercial-document halves; bank statements must not use this path."""
    documents = list(primary.documents) + list(secondary.documents)
    file_kind = primary.file_kind or secondary.file_kind
    return ReadDocumentBundle(
        file_kind=file_kind,
        document_count=len(documents),
        documents=documents,
        accounts=list(primary.accounts or []) + list(secondary.accounts or []),
    )


def _iter_pdf_halves(data: bytes) -> list[tuple[bytes, int, int]]:
    """Split a PDF into first/second page halves (1-based inclusive page labels)."""
    import io
    import os
    import tempfile
    from pathlib import Path

    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        total = len(pdf.pages) or 1
    if total < 2:
        return [(data, 1, total)]

    mid = total // 2
    try:
        import pypdfium2 as pdfium
    except ImportError:
        _log.warning("pypdfium2 unavailable — cannot split PDF for read_doc retry")
        return [(data, 1, total)]

    src = pdfium.PdfDocument(data)
    halves: list[tuple[bytes, int, int]] = []
    try:
        for start_idx, end_idx in ((0, mid - 1), (mid, total - 1)):
            out = pdfium.PdfDocument.new()
            try:
                out.import_pages(src, list(range(start_idx, end_idx + 1)))
                fd, path = tempfile.mkstemp(suffix=".pdf")
                os.close(fd)
                try:
                    out.save(path)
                    chunk_bytes = Path(path).read_bytes()
                finally:
                    Path(path).unlink(missing_ok=True)
                halves.append((chunk_bytes, start_idx + 1, end_idx + 1))
            finally:
                out.close()
    finally:
        src.close()
    return halves or [(data, 1, total)]


def read_bytes_with_retry(
    data: bytes,
    mime: str,
    *,
    read_once: ReadFn,
    lite_model: str,
    std_model: str,
) -> tuple[ReadDocumentBundle, dict[str, Any]]:
    """Single call by default; retry with std model, then logical PDF halves on failure."""
    models = [lite_model, std_model]
    last_exc: Exception | None = None
    usage_total: dict[str, Any] = {}
    call_count = 0

    for model in models:
        call_count += 1
        try:
            bundle, meta = read_once(data, mime, model)
            meta["gemini_call_count"] = call_count
            meta["models_tried"] = models[:call_count]
            for key, val in (meta.get("usage") or {}).items():
                if isinstance(val, (int, float)):
                    usage_total[key] = usage_total.get(key, 0) + val
            meta["usage"] = usage_total or meta.get("usage")
            return bundle, meta
        except ValidationError as exc:
            last_exc = exc
            _log.warning("read_doc ValidationError on model=%s — retrying", model)

    if mime != "application/pdf" or count_input_pages(data, mime) < 2:
        assert last_exc is not None
        raise last_exc

    halves = _iter_pdf_halves(data)
    if len(halves) < 2:
        assert last_exc is not None
        raise last_exc

    merged: ReadDocumentBundle | None = None
    for chunk_bytes, start_page, end_page in halves:
        call_count += 1
        try:
            bundle, meta = read_once(chunk_bytes, mime, std_model)
        except ValidationError as exc:
            last_exc = exc
            _log.warning(
                "read_doc half-chunk ValidationError pages %s-%s",
                start_page,
                end_page,
            )
            continue
        for key, val in (meta.get("usage") or {}).items():
            if isinstance(val, (int, float)):
                usage_total[key] = usage_total.get(key, 0) + val
        merged = _merge_bundles(merged, bundle) if merged else bundle

    if merged is None:
        assert last_exc is not None
        raise last_exc

    if merged.document_count != len(merged.documents):
        merged = merged.model_copy(update={"document_count": len(merged.documents)})

    return merged, {
        "gemini_call_count": call_count,
        "models_tried": models + [std_model] * len(halves),
        "extract_mode": "vision",
        "retry_strategy": "pdf_halves",
        "usage": usage_total,
    }


def gemini_read_once(
    client: Any,
    data: bytes,
    mime: str,
    model: str,
) -> tuple[ReadDocumentBundle, dict[str, Any]]:
    """One Gemini structured-output call; raises ValidationError on bad JSON."""
    import time

    from ledgr_agent.internal.gemini import usage_from_response

    part = types.Part.from_bytes(data=data, mime_type=mime)
    t0 = time.perf_counter()
    resp = client.models.generate_content(
        model=model,
        contents=[part, READ_PROMPT, BUNDLE_READER_INSTRUCTION],
        config=default_llm_config(
            temperature=0,
            response_mime_type="application/json",
            response_schema=ReadDocumentBundle,
        ),
    )
    elapsed = round(time.perf_counter() - t0, 2)
    bundle = ReadDocumentBundle.model_validate_json(resp.text or "{}")
    return bundle, {
        "model": model,
        "elapsed_seconds": elapsed,
        "bytes_sent": len(data),
        "usage": usage_from_response(resp),
    }
