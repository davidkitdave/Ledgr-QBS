"""One-call multi-document extraction for bills / SOA / multi-receipt packs."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from google.genai import types

from ledgr_agent.models.document_bundle import BUNDLE_READER_INSTRUCTION, FileKind, ReadDocumentBundle
from ledgr_agent.normalize.bank_statement import normalize_bank_statement
from ledgr_agent.shared.gemini_call_config import default_llm_config
from ledgr_agent.shared.gemini_usage import usage_from_response
from ledgr_agent.shared.genai_client import lite_model, make_client
from ledgr_agent.shared.mime import mime_for

_log = logging.getLogger(__name__)

_READ_PROMPT = (
    "Read the attached financial file. Decide whether it is a bank statement or "
    "commercial documents, then extract the matching fields in the output schema."
)


def read_document_bundle(path: str | Path) -> dict[str, Any]:
    """Read one file with ONE Gemini call.

    Gemini sets ``file_kind`` (bank vs commercial) and fills ``accounts`` or
    ``documents``. On success returns the parsed payload plus ``extraction_meta``.
    On failure returns ``{status: "error", message: ...}``.
    """
    doc_path = Path(path)
    try:
        data = doc_path.read_bytes()
    except OSError as exc:
        return {"status": "error", "message": f"Could not read {doc_path}: {exc}"}

    try:
        client = make_client()
        part = types.Part.from_bytes(data=data, mime_type=mime_for(doc_path))
        model = lite_model()
        t0 = time.perf_counter()
        resp = client.models.generate_content(
            model=model,
            contents=[part, _READ_PROMPT, BUNDLE_READER_INSTRUCTION],
            config=default_llm_config(
                temperature=0,
                response_mime_type="application/json",
                response_schema=ReadDocumentBundle,
            ),
        )
        elapsed = round(time.perf_counter() - t0, 2)
        bundle = ReadDocumentBundle.model_validate_json(resp.text or "{}")
        if bundle.document_count != len(bundle.documents):
            bundle = bundle.model_copy(update={"document_count": len(bundle.documents)})
    except Exception as exc:  # noqa: BLE001
        _log.exception("read_document_bundle failed for %s", doc_path)
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    payload = bundle.model_dump()
    file_kind: FileKind = payload.get("file_kind") or "commercial_documents"
    extract_mode = "vision"
    if file_kind == "bank_statement":
        payload["accounts_normalized"] = normalize_bank_statement(
            payload,
            extract_mode=extract_mode,
        )
    payload["extraction_meta"] = {
        "gemini_call_count": 1,
        "model": model,
        "extract_mode": extract_mode,
        "elapsed_seconds": elapsed,
        "bytes_sent": len(data),
        "usage": usage_from_response(resp),
        "source_path": str(doc_path),
    }
    return payload
