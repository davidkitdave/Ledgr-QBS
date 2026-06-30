"""Light-path document reader: one ``read_doc`` tool for bills and bank statements."""

from __future__ import annotations

import logging
import time
from typing import Any

from google.genai import types

from ledgr_agent.billing import (
    billable_units,
    estimate_units_from_bytes,
    gate as billing_gate,
)
from ledgr_agent.internal.gemini import (
    count_input_pages,
    default_llm_config,
    lite_model,
    make_client,
    mime_for,
    usage_from_response,
)
from ledgr_agent.internal.normalize import normalize_bank_statement
from ledgr_agent.internal.schemas import BUNDLE_READER_INSTRUCTION, ReadDocumentBundle
from ledgr_agent.internal.uploads import resolve_document_paths

_log = logging.getLogger(__name__)

_READ_PROMPT = (
    "Read the attached financial file. Decide whether it is a bank statement or "
    "commercial documents, then extract the matching fields in the output schema."
)

READ_DOC_STATE_KEY = "read_doc"


def _read_bytes_with_gemini(data: bytes, mime: str) -> dict[str, Any]:
    client = make_client()
    part = types.Part.from_bytes(data=data, mime_type=mime)
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
    payload = bundle.model_dump()
    file_kind = payload.get("file_kind") or "commercial_documents"
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
    }
    return payload


def read_doc(tool_context: Any, paths: list[str] | None = None) -> dict[str, Any]:
    """Read one uploaded financial file into structured JSON.

    Gemini sets ``file_kind`` (``bank_statement`` or ``commercial_documents``) and
    fills ``accounts`` or ``documents``. Call ``build_sheets`` next to produce
    workbook rows.

    Args:
        tool_context: ADK session context (playground / agents-cli uploads).
        paths: On-disk paths, or ``[]`` when a file is attached.

    Returns:
        Summary with ``file_kind``, counts, and ``extraction_meta``. Full payload
        is stored in session state under ``read_doc``.
    """
    path_list = list(paths or [])
    existing_paths, missing_files, resolution = resolve_document_paths(tool_context, path_list)

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

    mime = mime_for(doc_path)
    pre_gate_units = estimate_units_from_bytes(data, mime)
    blocked = billing_gate(tool_context, units=pre_gate_units, kind="bill")
    if blocked is not None:
        return {
            "status": "blocked",
            "message": "Insufficient credits to read this document.",
            "credits": blocked.model_dump(),
        }

    try:
        payload = _read_bytes_with_gemini(data, mime)
    except Exception as exc:  # noqa: BLE001
        _log.exception("read_doc failed for %s", doc_path)
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    file_kind = payload.get("file_kind") or "commercial_documents"
    page_count = max(count_input_pages(data, mime), 1)
    document_count = len(payload.get("documents") or [])
    credit_units = billable_units(
        file_kind=file_kind,
        page_count=page_count,
        document_count=document_count,
    )
    charge_kind = "bank" if file_kind == "bank_statement" else "bill"
    if credit_units > pre_gate_units:
        blocked = billing_gate(tool_context, units=credit_units, kind=charge_kind)
        if blocked is not None:
            return {
                "status": "blocked",
                "message": (
                    "Insufficient credits to read this bank statement."
                    if file_kind == "bank_statement"
                    else "Insufficient credits to read this document."
                ),
                "credits": blocked.model_dump(),
            }

    payload["source_path"] = str(doc_path)
    payload["page_count"] = page_count
    payload["document_count"] = document_count
    payload["credit_units"] = credit_units
    if resolution:
        payload["source_resolution"] = resolution

    if tool_context is not None and getattr(tool_context, "state", None) is not None:
        tool_context.state[READ_DOC_STATE_KEY] = payload

    summary: dict[str, Any] = {
        "status": "success",
        "file_kind": file_kind,
        "extraction_meta": payload.get("extraction_meta"),
        "source_path": payload["source_path"],
    }
    if file_kind == "bank_statement":
        accounts = payload.get("accounts") or []
        normalized = payload.get("accounts_normalized") or []
        summary.update(
            {
                "account_count": len(normalized) or len(accounts),
                "sheet_titles": [a.get("sheet_title") for a in normalized],
                "reconciled_all": all(a.get("reconciled") for a in normalized) if normalized else False,
            }
        )
    else:
        docs = payload.get("documents") or []
        summary["document_count"] = len(docs)
        summary["credit_units"] = credit_units
        if docs:
            first = docs[0]
            summary["vendor_name"] = first.get("vendor_name")
            summary["invoice_number"] = first.get("invoice_number")
    return summary
