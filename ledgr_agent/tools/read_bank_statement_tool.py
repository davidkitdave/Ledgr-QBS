"""Light-path bank statement reader as a FunctionTool."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ledgr_agent.extract.bank_statement import extract_bank_statement
from ledgr_agent.normalize.bank_statement import normalize_bank_statement
from ledgr_agent.tools.playground_uploads import resolve_document_paths

_log = logging.getLogger(__name__)


def _mime_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff"}:
        if suffix in {".jpg", ".jpeg"}:
            return "image/jpeg"
        return f"image/{suffix.lstrip('.')}"
    return "application/pdf"


def read_bank_statement(tool_context: Any, paths: list[str] | None = None) -> dict[str, Any]:
    """Read one bank statement PDF into structured JSON (light path).

    **Scope — use this tool when:**

    - Bank / account statement PDF or image
    - User wants workbook tabs via ``project_bank_workbook``
    - One statement file (multi-account / multi-currency inside is OK)

    **Do not use — route elsewhere:**

    - Commercial invoice / receipt / credit note → ``read_document``
    - SOA / multi-invoice PDF → ``process_document_batch``
    - Full FY merge across months → ``process_document_batch`` (for now)

    Hybrid extraction: digital PDFs use pdfplumber text + flash-lite; scans use vision.

    Args:
        tool_context: ADK session context (playground / agents-cli uploads).
        paths: On-disk paths, or ``[]`` when a file is attached.

    Returns:
        Summary with ``accounts``, ``account_count``, ``sheet_titles``, ``reconciled_all``,
        and ``extraction_meta``. Full normalized payload is stored in session state.
        On failure ``{status: "error", message: ...}``.
    """
    path_list = list(paths or [])
    existing_paths, missing_files, _resolution = resolve_document_paths(tool_context, path_list)

    if not existing_paths:
        message = (
            "No bank statement found. Attach a file (agents-cli --file or playground) "
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

    mime = _mime_for_path(doc_path)
    t0 = time.perf_counter()
    try:
        parsed, mode_used = extract_bank_statement(
            data,
            mime,
            path=doc_path if mime == "application/pdf" else None,
            mode="auto",
        )
    except Exception as exc:  # noqa: BLE001
        _log.exception("read_bank_statement failed for %s", doc_path)
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    elapsed = round(time.perf_counter() - t0, 2)
    payload = parsed.model_dump()
    accounts_normalized = normalize_bank_statement(payload, extract_mode=mode_used)
    full_state = {
        **payload,
        "accounts_normalized": accounts_normalized,
        "extraction_meta": {
            "gemini_call_count": 1,
            "extract_mode": mode_used,
            "elapsed_seconds": elapsed,
            "bytes_sent": len(data),
        },
    }
    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        tool_context.state["read_bank_statement"] = full_state
    return {
        "accounts": payload.get("accounts") or [],
        "account_count": len(accounts_normalized),
        "sheet_titles": [a.get("sheet_title") for a in accounts_normalized],
        "reconciled_all": all(a.get("reconciled") for a in accounts_normalized),
        "extraction_meta": full_state["extraction_meta"],
    }
