"""Light-path document reader as a FunctionTool (real PDF handoff).

``AgentTool`` only forwards a text ``request`` to the child agent — it does not
pass playground / ``agents-cli --file`` attachments. For structured extraction
ADK recommends a ``FunctionTool`` that loads the bytes and calls Gemini
directly (same pattern as ``extract_one_bill_minimal``).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from google.genai import types

from ledgr_agent.shared.gemini_call_config import default_llm_config
from ledgr_agent.shared.gemini_usage import usage_from_response
from ledgr_agent.shared.genai_client import lite_model, make_client
from ledgr_agent.agents.document_reader import READER_INSTRUCTION, ReadDocument
from ledgr_agent.tools.playground_uploads import resolve_document_paths

_log = logging.getLogger(__name__)

_READ_PROMPT = "Read the attached financial document."


def _mime_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff"}:
        if suffix in {".jpg", ".jpeg"}:
            return "image/jpeg"
        return f"image/{suffix.lstrip('.')}"
    return "application/pdf"


def read_document(tool_context: Any, paths: list[str] | None = None) -> dict[str, Any]:
    """Read one commercial bill into structured JSON (light path).

    **Scope — use this tool when:**

    - Single invoice, tax invoice, receipt, or credit note
    - User wants ERP import rows via ``project_to_erp`` (QBS / Xero / AutoCount / SQL)
    - One logical document per file (not SOA / multi-invoice packs)

    **Do not use — route elsewhere:**

    - Bank statement PDF → ``read_bank_statement`` (Phase 2; use factory until wired)
    - Statement of account / multi-invoice PDF → ``process_document_batch``
    - Multi-file batch, credit gating, COA categorization, tax engine → ``process_document_batch``
    - Fast single-bill extract with SR/ZR tax breakdown only → ``extract_one_bill_minimal``

    Args:
        tool_context: ADK session context (used to recover playground uploads).
        paths: File paths on disk. In agents-cli / ADK playground, pass ``[]``
            when the user attached a file; uploaded bytes are recovered from the
            session (same as ``process_document_batch``).

    Returns:
        A plain dict matching :class:`~ledgr_agent.agents.document_reader.ReadDocument`.
        On failure, ``{status: "error", message: ...}``. On success the result is
        also stored in session state under ``read_document``.
    """
    path_list = list(paths or [])
    existing_paths, missing_files, _resolution = resolve_document_paths(tool_context, path_list)

    if not existing_paths:
        message = (
            "No document found. Attach a file (agents-cli --file or playground upload) "
            "or pass valid paths."
        )
        if missing_files:
            message += f" Missing paths: {missing_files}"
        return {"status": "error", "message": message}

    doc_path = existing_paths[0]
    try:
        data = doc_path.read_bytes()
    except OSError as exc:
        return {"status": "error", "message": f"Could not read {doc_path}: {exc}"}

    try:
        client = make_client()
        part = types.Part.from_bytes(data=data, mime_type=_mime_for_path(doc_path))
        model = lite_model()
        t0 = time.perf_counter()
        resp = client.models.generate_content(
            model=model,
            contents=[part, _READ_PROMPT, READER_INSTRUCTION],
            config=default_llm_config(
                temperature=0,
                response_mime_type="application/json",
                response_schema=ReadDocument,
            ),
        )
        elapsed = round(time.perf_counter() - t0, 2)
        doc = ReadDocument.model_validate_json(resp.text or "{}")
    except Exception as exc:  # noqa: BLE001
        _log.exception("read_document failed for %s", doc_path)
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    payload = doc.model_dump()
    payload["extraction_meta"] = {
        "gemini_call_count": 1,
        "model": model,
        "elapsed_seconds": elapsed,
        "bytes_sent": len(data),
        "usage": usage_from_response(resp),
    }
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        tool_context.state["read_document"] = payload
    return payload
