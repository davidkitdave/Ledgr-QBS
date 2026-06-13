"""Dispatch tools for the Ledgr coordinator (front-desk) agent.

Each tool is a thin wrapper over the EXISTING deterministic engine
(``invoice_processing.pipeline`` + ``classify`` / ``extract`` / ``export``).
The coordinator LlmAgent decides *which* tool to call from the user's message;
these tools do the real work by delegating to the pipeline.

IMPORTANT: a file the user uploads (in the playground, ``adk web``, or Slack)
arrives as *bytes* -- an inline ``Part`` in the message and/or a saved artifact --
NOT as a filesystem path. These tools therefore read uploaded files from the
``ToolContext`` (``user_content`` parts + session artifacts), never from a path
the model invents. An optional explicit ``file_paths`` is still accepted for
local command-line runs (e.g. ``agents-cli run``).

Engine imports are done inside the functions so importing the agent (playground /
``adk web``) does not eagerly load the pipeline.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from google.adk.tools import ToolContext

# mime <-> extension helpers (kept local so this module imports nothing heavy)
_MIME_EXT = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_EXT_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _ext_for(name: str | None, mime: str | None) -> str:
    """Best file extension for a temp file, from the name then the mime type."""
    if name and "." in Path(name).name:
        return Path(name).suffix.lower()
    return _MIME_EXT.get((mime or "").lower(), ".bin")


def _mime_for_path(p: Path) -> str:
    return _EXT_MIME.get(p.suffix.lower(), "application/octet-stream")


async def _gather_documents(
    tool_context: ToolContext, file_paths: list[str] | None = None
) -> list[tuple[str, bytes, str]]:
    """Collect every document the user supplied, as ``(name, data, mime_type)``.

    Sources, de-duplicated by (name, size):
    1. Explicit ``file_paths`` that exist on disk (local CLI use).
    2. Inline files in the current message (``tool_context.user_content.parts``)
       -- this is how a same-turn upload arrives.
    3. Session artifacts (``list_artifacts`` / ``load_artifact``) -- how ``adk web``
       persists an upload so it is still reachable on a later turn.
    """
    found: list[tuple[str, bytes, str]] = []
    seen: set[tuple[str, int]] = set()

    def _add(name: str, data: bytes | None, mime: str | None) -> None:
        if not data:
            return
        key = (name, len(data))
        if key in seen:
            return
        seen.add(key)
        found.append((name, data, mime or "application/octet-stream"))

    # 1) explicit local paths
    for p in file_paths or []:
        fp = Path(p)
        if fp.exists():
            _add(fp.name, fp.read_bytes(), _mime_for_path(fp))

    # 2) inline files in the current user message
    uc = getattr(tool_context, "user_content", None)
    for i, part in enumerate(getattr(uc, "parts", None) or []):
        blob = getattr(part, "inline_data", None)
        if blob is not None and getattr(blob, "data", None):
            name = getattr(blob, "display_name", None) or f"upload_{i}"
            _add(name, blob.data, getattr(blob, "mime_type", None))

    # 3) session artifacts (persist across turns)
    try:
        names = await tool_context.list_artifacts()
    except Exception:  # noqa: BLE001 - no artifact service / not configured
        names = []
    for name in names or []:
        try:
            part = await tool_context.load_artifact(filename=name)
        except Exception:  # noqa: BLE001
            part = None
        blob = getattr(part, "inline_data", None) if part is not None else None
        if blob is not None and getattr(blob, "data", None):
            _add(name, blob.data, getattr(blob, "mime_type", None))

    return found


_NO_FILE = {
    "status": "no_file",
    "message": (
        "I don't see an attached document. Please upload a PDF or an image "
        "(invoice, bill, receipt, or bank statement) and try again."
    ),
}


def capabilities() -> dict:
    """List what Ledgr can do for the user.

    Call this when the user greets you, asks "what can you do", asks for help, or
    sends a message you cannot map to another tool. Always present this as a
    friendly menu so the user is never left without a next step.
    """
    return {
        "assistant": "Ledgr -- your Slack bookkeeping assistant for Singapore & Malaysia",
        "i_can": [
            "Turn accounting documents (invoices, bills, receipts, bank statements) "
            "into a categorised ledger workbook.",
            "Tell you what a document is before processing it (type, who issued it, amount).",
            "Explain how to set up a client channel and upload a Chart of Accounts.",
        ],
        "how_to_start": (
            "Upload a document and ask me to process it. In Slack, drop documents into "
            "your client channel; use `/ledgr settings` to set up, or `/ledgr export` to "
            "re-send your latest workbook."
        ),
    }


async def inspect_document(
    tool_context: ToolContext, file_paths: list[str] | None = None
) -> dict:
    """Classify the document(s) the user uploaded WITHOUT processing them.

    Returns, per document: type (invoice / receipt / bank_statement / credit_note /
    statement_of_account / other), who issued it, who it is billed to, currency,
    total amount, and a confidence score. Use this when the user asks "what is
    this?" or wants a quick look before a full run.

    The uploaded file is read automatically from the conversation -- do NOT pass or
    invent a file path. (`file_paths` is only for local command-line runs.)
    """
    from invoice_processing.classify.document_classifier import classify_document

    docs = await _gather_documents(tool_context, file_paths)
    if not docs:
        return dict(_NO_FILE)

    results = []
    for name, data, mime in docs:
        try:
            r = classify_document(data, mime)
            results.append(
                {
                    "file": name,
                    "doc_type": r.doc_type,
                    "issuer": r.issuer_name,
                    "bill_to": r.bill_to_name,
                    "currency": r.currency,
                    "total_amount": r.total_amount,
                    "confidence": r.confidence,
                    "reason": r.reason,
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append({"file": name, "error": str(exc)})
    return {"status": "ok", "documents": results}


async def process_documents(
    tool_context: ToolContext, file_paths: list[str] | None = None
) -> dict:
    """Run the full bookkeeping pipeline on the document(s) the user uploaded.

    Pipeline: classify -> extract -> categorise (COA) -> tax code -> reconcile ->
    route to financial year -> consolidate into workbook(s). Returns a per-document
    summary and the names of the workbook(s) produced.

    The uploaded file is read automatically from the conversation -- do NOT pass or
    invent a file path. (`file_paths` is only for local command-line runs.) In Slack
    this runs on upload using the channel's real client profile; here a demo profile
    is used.
    """
    docs = await _gather_documents(tool_context, file_paths)
    if not docs:
        return dict(_NO_FILE)

    from invoice_processing.export.client_context import ClientContext
    from invoice_processing.pipeline import process_batch

    tmp = tempfile.mkdtemp(prefix="ledgr_pg_")
    try:
        local: list[str] = []
        for name, data, mime in docs:
            dest = Path(tmp) / Path(name).name
            if dest.suffix == "":
                dest = dest.with_suffix(_ext_for(name, mime))
            dest.write_bytes(data)
            local.append(str(dest))

        demo = ClientContext(
            client_id="demo",
            client_name="Demo Pte Ltd",
            status="active",
            tax_registered=True,
            fye_month=12,
        )
        result = process_batch(local, demo)
        return {
            "status": "ok",
            "documents": [
                {
                    "file": Path(d.path).name,
                    "doc_type": d.doc_type,
                    "direction": d.direction,
                    "reconciled": d.reconciled,
                    "bucket": getattr(d.route, "bucket", None),
                    "fy": getattr(d.route, "fy", None),
                    "note": d.note,
                }
                for d in result.docs
            ],
            "workbooks": list(result.workbooks.keys()),
            "errors": result.errors,
            "note": (
                "Demo client profile used (no Chart of Accounts mapped). In Slack, your "
                "channel's real profile is applied."
            ),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
