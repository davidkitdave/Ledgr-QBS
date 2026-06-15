"""Slack Bolt → ADK Runner driver (the ONLY Slack-aware layer).

Architecture
------------
The ADK graph (``accounting_agents.agent``) is Slack-agnostic: its nodes only
read the uploaded PDF from the artifact service and write a serializable
``state["ledger_rows"]`` payload + a delivery summary. THIS module owns the Slack
client and performs every Slack I/O:

* download the dropped PDF (parked SSRF-hardened ``app.slack_app.slack_download_file``
  → bytes), ``save_artifact("inbox/{file_id}.pdf")``, seed run state, and drive
  ``runner.run_async``;
* on a HITL interrupt (``RequestInput`` surfacing as an ``adk_request_input``
  FunctionCall) post the Approve/Edit/Reject card (parked ``app.blocks``) and
  persist the correlation doc (``hitl.write_interrupt``);
* on normal completion, persist the run's ledger rows to the channel FY workbook
  via :class:`SlackLedgerStore <accounting_agents.ledger_store.SlackLedgerStore>`
  and post the delivery summary;
* button actions ``approve`` / ``edit`` / ``reject`` ``ack()`` immediately
  (< 3s) then ``resume_session`` out-of-band, run the same ledger-append + deliver
  path, and ``chat_update`` the card with the outcome (idempotent on double-click).

Session convention: ``user_id == channel_id`` (so the coordinator's
``before_agent_callback``, which reads ``state["channel_id"]``, resolves the
client by channel), while ``session_id`` is UNIQUE per document
(``f"{channel_id}:{file_id}"``) / per question (``f"{channel_id}:q:{message_ts}"``)
so simultaneous drops in the same channel never share session state. The HITL
interrupt doc carries that per-doc ``session_id`` (and ``user_id``) so resume
targets the exact paused session.

The heavy lifting lives in module-level pure functions (``process_file_event`` /
``handle_approval_action`` / ``persist_and_deliver``) that take the runner,
store, Slack client, and Firestore db as explicit args, so they are unit-testable
with fakes and no live Slack/Gemini.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import tempfile
from typing import Any, Optional

# FastAPI Request/Response imported at module level so FastAPI can resolve
# the string annotations produced by `from __future__ import annotations`.
from fastapi import Request, Response

from accounting_agents.assistant import LEDGER_DATA_KEY

from google.genai import types

from accounting_agents import nodes
from accounting_agents.hitl import (
    REQUEST_INPUT_FUNCTION_CALL_NAME,
    is_processed,
    read_interrupt,
    resume_session,
    update_interrupt_status,
    write_interrupt,
)
from accounting_agents.ledger_store import SlackLedgerStore
from accounting_agents.nodes import ApproveDecision
from app.blocks import approval_card_blocks, approval_outcome_blocks, invoice_edit_modal
from app.slack_app import _SeenEvents
from invoice_processing.export.client_context import FirestoreClientStore

# One shared dedup set for all handlers in this module (process-local; see
# app.slack_app._SeenEvents for the Cloud Run note about multi-instance gaps).
_seen = _SeenEvents()

# Futures for process_file_event results so the batch tally loop can await
# files already being processed by the file_shared handler.
_file_futures: dict[str, asyncio.Future] = {}

#: Default client store for profile seeding (overridable in tests).
_DEFAULT_CLIENT_STORE = FirestoreClientStore()


def _profile_state_delta(client_store, channel_id: str) -> dict:
    """Return the client's ``to_state()`` keys for seeding the run, or ``{}``.

    The coordinator's ``before_agent_callback`` does not reliably propagate the
    profile into the document lane, so the runner seeds it directly at run start
    (alongside ``channel_id``). Empty dict means "no profile for this channel" —
    callers soft-gate on that. See ADR-0005 (2026-06-14 addendum).
    """
    ctx = client_store.get_by_channel(channel_id)
    if ctx is None:
        return {}
    return ctx.to_state()

logger = logging.getLogger(__name__)

#: Artifact filename convention shared with the graph (``nodes.ARTIFACT_NAME_FMT``).
ARTIFACT_NAME_FMT = nodes.ARTIFACT_NAME_FMT

#: Extensions (and their MIME types) that the classifier + extractors accept.
#: Mirrors ``_MIME_BY_EXT`` in ``invoice_processing/classify/document_classifier.py``
#: and ``invoice_processing/extract/invoice_extractor.py`` exactly — do not add
#: types here without also updating those modules.
_ACCEPTED_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif",
})


def _validate_download(data: bytes, source_filename: str) -> Optional[str]:
    """Return a rejection reason string if ``data`` is unreadable, else ``None``.

    Two checks (in order):
    1. Non-empty: zero-byte downloads cannot be parsed by any extractor.
    2. Known extension: the filename extension must be one the pipeline's
       classifier and extractors support.  An unknown extension means the file
       type is unsupported (e.g. ``.exe``, ``.csv``, no extension).

    Returns ``None`` when the upload passes both checks (safe to proceed).
    Returns a human-readable reason string on failure so the caller can include
    it in the Slack rejection message.
    """
    if not data:
        return "the file appears to be empty"
    from pathlib import Path as _Path
    ext = _Path(source_filename).suffix.lower()
    if ext not in _ACCEPTED_EXTENSIONS:
        supported = ", ".join(sorted(_ACCEPTED_EXTENSIONS))
        return (
            f"the file type is not supported "
            f"(got `{ext or 'no extension'}`; supported: {supported})"
        )
    return None


def _max_concurrency() -> int:
    """Max documents/questions processed concurrently per process (env-tunable)."""
    try:
        return max(1, int(os.environ.get("LEDGR_MAX_CONCURRENCY", "5")))
    except (TypeError, ValueError):
        return 5


#: Backpressure: at most N runs in flight per process so simultaneous drops queue
#: in-memory instead of stampeding the Gemini API. Tunable via ``LEDGR_MAX_CONCURRENCY``.
_SEM = asyncio.Semaphore(_max_concurrency())


def _per_doc_session_id(channel_id: str, file_id: str) -> str:
    """Unique session id per dropped document so concurrent drops never collide."""
    return f"{channel_id}:{file_id}"


def _is_coa_upload(f: object, *, coa_pending: bool) -> bool:
    """COA-routing decision (ADR-0006 path A) for a dropped Slack file.

    A spreadsheet (xlsx/csv) dropped on a channel that is not yet onboarded or is
    ``pending_coa`` is a Chart-of-Accounts upload → route to ``run_coa_ingest``.
    Everything else (any file on an active client, or any non-spreadsheet) is an
    ordinary document → route to ``process_file_event``. This is the live
    replacement for the retired ``app.slack_app.handle_file_share`` discriminator.
    """
    if not coa_pending or not isinstance(f, dict):
        return False
    from app.slack_app import _is_spreadsheet
    return bool(_is_spreadsheet(f))


def _per_question_session_id(channel_id: str, message_ts: str) -> str:
    """Unique session id per question message so concurrent questions never collide.

    DEPRECATED for the chat path; kept for backward compatibility. The chat lane
    now uses :func:`_chat_session_id` (per-thread + day-bucket fallback) so
    multi-turn history accumulates instead of being thrown away per question.
    """
    return f"{channel_id}:q:{message_ts}"


def _chat_session_id(
    channel_id: str,
    raw_thread_ts: Optional[str],
    message_ts: Optional[str],
) -> str:
    """Per-thread chat session id with a day-bucket fallback (ADR-0008).

    Rules:
    - If ``raw_thread_ts`` is truthy (the message is inside a Slack thread, i.e.
      the raw ``event["thread_ts"]`` was set) → ``{channel}:chat:{raw_thread_ts}``
      so every reply in that thread reuses the same multi-turn session.
    - Otherwise derive a UTC day from ``message_ts`` (Slack's event ts, a float
      string of seconds since epoch) and return ``{channel}:chat:day-{YYYY-MM-DD}``
      so a series of top-level messages on the same day share one session.
    - If ``message_ts`` is missing or unparseable, fall back to ``{channel}`` so
      the chat path still works (lower granularity is acceptable for the rare
      direct-call case used by tests).

    Deterministic (driven by Slack's ``ts``, not wall clock) and timezone-stable
    so tests can pin a known UTC day.
    """
    if raw_thread_ts:
        return f"{channel_id}:chat:{raw_thread_ts}"
    try:
        day = (
            datetime.datetime.utcfromtimestamp(float(message_ts))
            .date()
            .isoformat()
        )
    except (TypeError, ValueError):
        return channel_id
    return f"{channel_id}:chat:day-{day}"


# --------------------------------------------------------------------------- #
# Event-stream inspection helpers
# --------------------------------------------------------------------------- #


def extract_final_text(event: Any) -> str:
    """Extract text from a final ADK event's content parts.

    FIX (reply-capture bug): read the per-part ``text`` and join — NEVER read
    ``content.text`` (which does not exist on multi-part content and silently
    drops the model's reply). Returns "" when the event has no text parts.
    """
    content = getattr(event, "content", None)
    if content is None:
        return ""
    parts = getattr(content, "parts", None) or []
    return "".join(p.text for p in parts if getattr(p, "text", None))


def extract_tool_response_text(event: Any) -> str:
    """Return the latest function-response ``result`` text on ``event``, or "".

    Safety net for the chat lane: ``gemini-2.5-flash-lite`` occasionally goes
    silent after a tool call (emits a final event with no text parts), even
    with an instruction telling it to always reply. When that happens we'd
    rather surface the tool's raw result to the user than the opaque
    "rephrase your question" canned message — they asked, the tool answered,
    they should see it.
    """
    getter = getattr(event, "get_function_responses", None)
    responses = getter() if callable(getter) else []
    for fr in reversed(responses or []):
        resp = getattr(fr, "response", None)
        if isinstance(resp, dict):
            result = resp.get("result")
            if isinstance(result, str) and result.strip():
                return result
    return ""


def find_interrupt_id(event: Any) -> Optional[str]:
    """Return the interrupt id if ``event`` carries an ``adk_request_input`` call."""
    getter = getattr(event, "get_function_calls", None)
    calls = getter() if callable(getter) else []
    for fc in calls or []:
        if getattr(fc, "name", None) == REQUEST_INPUT_FUNCTION_CALL_NAME:
            return fc.id
    return None


#: Maps a graph node name to the friendly status label shown while it runs. The
#: ADK workflow tags each event's ``node_info.path`` with the producing node
#: (e.g. ``"document_workflow@1/classify_node@1"``); :func:`event_node_name`
#: pulls the trailing node name and we look it up here to drive the live status.
_STAGE_LABELS: dict[str, str] = {
    "classify_node": "🔍 Taking a look at this document…",
    "extract_invoice_node": "🧾 Looks like an invoice — reading the line items…",
    "extract_bank_node": "🏦 Looks like a bank statement — reading each transaction…",
    "categorize_node": "🗂️ Matching each line to your chart of accounts…",
    "tax_node": "🧮 Checking the tax treatment and reconciling…",
    "approval_gate": "🧮 Checking the tax treatment and reconciling…",
    "route_node": "🧭 Working out where this belongs…",
    "consolidate_node": "📒 Writing it into your workbook…",
    "deliver_node": "📦 Wrapping up…",
}


def event_node_name(event: Any) -> Optional[str]:
    """Return the graph node that produced ``event`` (e.g. ``"classify_node"``).

    The ADK ``Workflow`` stamps every node event's ``node_info.path`` with a
    ``"<workflow>@<n>/<node>@<n>"`` path; we take the trailing path segment and
    strip the ``@<rev>`` suffix. Returns ``None`` when the event is not a
    node-tagged event (no ``node_info`` / empty path), so callers can ignore it.
    """
    node_info = getattr(event, "node_info", None)
    path = getattr(node_info, "path", None) if node_info is not None else None
    if not path:
        return None
    last = str(path).rsplit("/", 1)[-1]
    return last.split("@", 1)[0] or None


def event_stage_label(event: Any) -> Optional[str]:
    """Return the friendly status label for ``event``'s node, or ``None``.

    ``None`` means "no stage transition to show" — either the event was not
    produced by a mapped node, so the live status message is left untouched.
    """
    name = event_node_name(event)
    if name is None:
        return None
    return _STAGE_LABELS.get(name)


def deslugify_channel_name(name: str) -> str:
    """Turn a Slack channel slug into a human client name for modal pre-fill.

    ``"akar-enterprises-pte-ltd"`` → ``"Akar Enterprises Pte Ltd"``. Splits on
    ``-``/``_``, title-cases each word, then restores conventional casing for
    common company-suffix tokens (``Pte Ltd``, ``LLP``, ``Pte``, ``Ltd``…).
    Returns ``""`` for an empty/whitespace-only name.
    """
    if not name:
        return ""
    words = [w for w in name.replace("_", "-").replace("-", " ").split() if w]
    if not words:
        return ""
    # Conventional casing overrides for common company-suffix tokens; anything
    # not listed falls back to plain title-case.
    _SUFFIX_CASE = {
        "pte": "Pte", "ltd": "Ltd", "inc": "Inc", "co": "Co",
        "llp": "LLP", "llc": "LLC", "plc": "PLC",
        "sg": "SG", "my": "MY",
    }
    return " ".join(_SUFFIX_CASE.get(w.lower(), w[:1].upper() + w[1:].lower()) for w in words)


# --------------------------------------------------------------------------- #
# Ledger persistence + delivery (shared by the file path and the resume path)
# --------------------------------------------------------------------------- #


async def persist_and_deliver(
    *,
    runner: Any,
    ledger_store: SlackLedgerStore,
    slack_client: Any,
    channel_id: str,
    session_id: str,
    app_name: str,
    user_id: Optional[str] = None,
    thread_ts: Optional[str] = None,
) -> dict:
    """Read the finished session's ledger payload → append to the FY workbook → post.

    Reads ``state["ledger_rows"]`` (prepared Slack-agnostically by
    ``consolidate_node``) from the persisted session and, when present, calls
    :meth:`SlackLedgerStore.append_rows` to fetch/append/re-upload the channel's
    FY workbook. Then posts the ``deliver_summary`` text. Returns the append
    result (or an empty dict when there was nothing to persist).

    ``session_id`` is the per-document session id; ``user_id`` is the ADK user id
    the session is stored under (the ``channel_id`` by convention). It defaults to
    ``session_id`` for backward-compatible single-id callers.
    """
    if user_id is None:
        user_id = session_id
    session = await runner.session_service.get_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )
    state = dict(session.state) if session else {}
    payload = state.get(nodes.LEDGER_ROWS_KEY) or {}
    batches = payload.get("batches") or []

    append_result: dict = {}
    if batches:
        append_result = await asyncio.to_thread(
            ledger_store.append_rows,
            client_id=payload.get("client_id") or "unknown",
            fy=str(payload.get("fy") or "unknown"),
            slack_client=slack_client,
            channel_id=channel_id,
            batches=batches,
            software=payload.get("software") or "qbs",
            kind=payload.get("kind") or "invoice",
            client_name=payload.get("client_name") or "",
        )
        # Carry context forward so the batch tally can label the destination
        # accurately (bank statement vs ledger) without re-reading the payload.
        append_result.setdefault("kind", payload.get("kind") or "invoice")
        append_result.setdefault("software", payload.get("software") or "")
        append_result.setdefault("fy", str(payload.get("fy") or ""))

    # When every batch was deduped (already in seen_doc_keys), skip the
    # agent-generated summary ("Added Sep 2025 …") and explain the dedup in the
    # same warm voice — naming WHAT is already recorded (month / invoice no.).
    if append_result.get("deduped", 0) > 0 and append_result.get("appended", 0) == 0:
        kind = payload.get("kind") or "invoice"
        if kind == "bank":
            where = "bank statement"
            all_rows = [r for b in batches for r in (b.get("rows") or [])]
            label = nodes._month_label(all_rows)
        else:
            where = "ledger"
            dk = str(batches[0].get("doc_key") or "") if batches else ""
            label = dk.split(":")[-1] if ":" in dk else ""
        named = f"**{label}** " if label else "this document "
        _post_message(
            slack_client, channel_id,
            f"📋 I already have {named}in your {where} — nothing new to add.\n"
            'Want me to replace it? Re-upload the file with a note like "re-process this".',
            thread_ts=thread_ts,
        )
        append_result["all_deduped"] = True
        return append_result

    summary = state.get(nodes.DELIVER_SUMMARY_KEY) or "Document processed."
    _post_message(slack_client, channel_id, summary, thread_ts=thread_ts)
    return append_result


def _post_message(slack_client: Any, channel_id: str, text: str, thread_ts=None) -> None:
    kwargs = {"channel": channel_id, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    slack_client.chat_postMessage(**kwargs)


# --------------------------------------------------------------------------- #
# Instant-ack helpers: reaction add/remove + message-ts resolution
# --------------------------------------------------------------------------- #


def _resolve_file_message_ts(slack_client: Any, file_id: str, channel_id: str) -> Optional[str]:
    """Return the ts of the user's upload message for ``file_id`` in ``channel_id``.

    ``files_info`` returns ``file.shares.{public,private}[channel_id][0].ts``.
    Handles both share buckets; returns ``None`` on any error or missing data so
    callers can fall back gracefully — this is cosmetic, never blocking.
    """
    try:
        resp = slack_client.files_info(file=file_id)
        data = resp.data if hasattr(resp, "data") else resp
        if not isinstance(data, dict):
            return None
        file_obj = data.get("file") or {}
        shares = file_obj.get("shares") or {}
        for bucket in ("private", "public"):
            channel_shares = (shares.get(bucket) or {}).get(channel_id)
            if channel_shares:
                ts = channel_shares[0].get("ts")
                if ts:
                    return ts
    except Exception:  # noqa: BLE001 - cosmetic
        logger.debug("files_info failed for file %s channel %s", file_id, channel_id)
    return None


def _resolve_file_name(slack_client: Any, file_id: str, file_obj: Optional[dict] = None) -> str:
    """Best-effort REAL uploaded filename for a Slack file.

    The extension drives :func:`_validate_download` and the name labels every
    review card. ``message``/``file_share`` events carry the full file object
    (with ``name``); ``file_shared`` events may not, so we fall back to
    ``files_info``. Returns ``"document.pdf"`` only when the name is truly
    unavailable — NEVER hard-code this elsewhere, or validation always sees a
    supported ``.pdf`` extension and can't reject unsupported uploads (and cards
    all read "document.pdf"). See ADR / QA 2026-06-14.
    """
    name = file_obj.get("name") if isinstance(file_obj, dict) else None
    if not name:
        try:
            resp = slack_client.files_info(file=file_id)
            data = resp.data if hasattr(resp, "data") else resp
            if isinstance(data, dict):
                name = (data.get("file") or {}).get("name")
        except Exception:  # noqa: BLE001 - fall back to default below
            logger.debug("files_info(name) failed for file %s", file_id)
    return name or "document.pdf"


def _add_reaction(slack_client: Any, channel_id: str, ts: Optional[str], name: str) -> None:
    """Add an emoji reaction to a message. Cosmetic: any error is swallowed."""
    if not ts:
        return
    try:
        slack_client.reactions_add(channel=channel_id, timestamp=ts, name=name)
    except Exception:  # noqa: BLE001 - cosmetic
        logger.debug("reactions_add(%s) failed for %s/%s", name, channel_id, ts)


def _remove_reaction(slack_client: Any, channel_id: str, ts: Optional[str], name: str) -> None:
    """Remove an emoji reaction from a message. Cosmetic: any error is swallowed."""
    if not ts:
        return
    try:
        slack_client.reactions_remove(channel=channel_id, timestamp=ts, name=name)
    except Exception:  # noqa: BLE001 - cosmetic
        logger.debug("reactions_remove(%s) failed for %s/%s", name, channel_id, ts)


# --------------------------------------------------------------------------- #
# Live status message (posted on drop, edited in-place as the run progresses)
# --------------------------------------------------------------------------- #


def _post_status(
    slack_client: Any, channel_id: str, text: str, thread_ts=None
) -> Optional[str]:
    """Post the initial live-status message and return its ``ts`` (or ``None``).

    Cosmetic-only: a failure here must never abort document processing, so any
    Slack error is logged and swallowed (the run continues silently).
    """
    kwargs = {"channel": channel_id, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    try:
        resp = slack_client.chat_postMessage(**kwargs)
    except Exception:  # noqa: BLE001 - status post is cosmetic
        logger.exception("failed to post status message in %s", channel_id)
        return None
    data = resp.data if hasattr(resp, "data") else resp
    if isinstance(data, dict):
        return data.get("ts")
    return None


def _update_status(slack_client: Any, channel_id: str, ts: Optional[str], text: str) -> None:
    """Edit the live-status message in place. No-op when ``ts`` is missing.

    Cosmetic-only: a failed ``chat_update`` is logged and swallowed so it can
    never crash the run (real processing errors are raised elsewhere).
    """
    if not ts:
        return
    try:
        slack_client.chat_update(channel=channel_id, ts=ts, text=text)
    except Exception:  # noqa: BLE001 - status update is cosmetic
        logger.exception("failed to update status message in %s", channel_id)


# --------------------------------------------------------------------------- #
# file_shared → run the document workflow
# --------------------------------------------------------------------------- #


async def process_file_event(
    *,
    runner: Any,
    ledger_store: SlackLedgerStore,
    db: Any,
    slack_client: Any,
    channel_id: str,
    file_id: str,
    app_name: str,
    download_fn,
    source_filename: str = "document.pdf",
    thread_ts: Optional[str] = None,
    client_store=None,
) -> dict:
    """Download the dropped PDF, run the workflow, and persist OR pause for HITL.

    Steps:
    1. ``download_fn(slack_client, file_id) -> bytes`` (the parked SSRF-hardened
       downloader, adapted to return bytes).
    2. ``save_artifact("inbox/{file_id}.pdf")`` into the runner's artifact service.
    3. ``runner.run_async(user_id=channel_id, session_id="{channel_id}:{file_id}", ...)``
       with ``state_delta`` seeding ``channel_id`` / ``file_id`` / ``source_filename``
       / the artifact name.
    4. Stream events: if a HITL interrupt appears → ``write_interrupt`` + post the
       Approve/Edit/Reject card and STOP (workflow paused). Otherwise on normal
       completion → :func:`persist_and_deliver`.

    Returns a small status dict: ``{"status": "paused"|"delivered", ...}``.

    Concurrency: each dropped document gets a UNIQUE per-doc session id
    (``f"{channel_id}:{file_id}"``) while ``user_id`` stays the ``channel_id`` so
    the client-profile ``before_agent_callback`` (which resolves by channel/user)
    is unchanged. The body runs under a module-level semaphore so simultaneous
    drops queue rather than stampede.
    """
    # Per-document session id so concurrent drops never share a session; user_id
    # stays channel_id (the before_agent_callback resolves the client by channel).
    session_id = _per_doc_session_id(channel_id, file_id)

    # Soft-gate on client profile: if this channel has not been onboarded yet,
    # there is no software target and no COA — the run would silently default
    # to QBS and write the wrong columns. Tell the user how to set up first.
    client_store = client_store or _DEFAULT_CLIENT_STORE
    profile_delta = _profile_state_delta(client_store, channel_id)
    if not profile_delta or not profile_delta.get("software"):
        _post_message(
            slack_client, channel_id,
            "I don't have this client set up yet — run */ledgr settings* to choose "
            "the accounting software and financial year, then re-drop the document.",
        )
        return {"status": "no_profile", "channel_id": channel_id, "file_id": file_id}

    # ── INSTANT ACK (before semaphore, before download) ──────────────────────
    # Resolve the user's upload-message ts so we can react to it immediately.
    # Falls back to None; if so, we still post the status message (also instant).
    upload_msg_ts = _resolve_file_message_ts(slack_client, file_id, channel_id)
    _add_reaction(slack_client, channel_id, upload_msg_ts, "eyes")

    # Live status message: post immediately on drop so the user sees the bot is
    # working, then edit it in place through the run (see _update_status). Both
    # the reaction and this post are OUTSIDE the semaphore so even a queued drop
    # that is waiting on _SEM still shows an instant "on it" signal to the user.
    status_ts = _post_status(
        slack_client, channel_id, f"📥 Received `{source_filename}` — on it…", thread_ts
    )

    async with _SEM:
        data = download_fn(slack_client, file_id)

        # Validate before touching the graph: reject empty files and unsupported
        # extensions so garbage never reaches Gemini and is not counted as
        # "processed" in the batch-drop tally.
        rejection_reason = _validate_download(data, source_filename)
        if rejection_reason is not None:
            logger.warning(
                "rejected unreadable upload: file=%s channel=%s reason=%s",
                file_id, channel_id, rejection_reason,
            )
            _update_status(slack_client, channel_id, status_ts, "❌ Couldn't read this file")
            _post_message(
                slack_client, channel_id,
                f"Sorry, I couldn't read `{source_filename}` — {rejection_reason}. "
                "Please re-upload a supported document (PDF, PNG, JPG, WEBP, or GIF).",
                thread_ts=thread_ts,
            )
            return {
                "status": "rejected_unreadable",
                "channel_id": channel_id,
                "file_id": file_id,
                "reason": rejection_reason,
            }

        artifact_name = ARTIFACT_NAME_FMT.format(file_id=file_id)

        await runner.artifact_service.save_artifact(
            app_name=app_name,
            user_id=channel_id,
            session_id=session_id,
            filename=artifact_name,
            artifact=types.Part(
                inline_data=types.Blob(data=data, mime_type="application/pdf")
            ),
        )

        await _ensure_session(runner, app_name, channel_id, session_id)

        state_delta = {
            "channel_id": channel_id,
            "file_id": file_id,
            "source_filename": source_filename,
            nodes.ARTIFACT_NAME_KEY: artifact_name,
            **profile_delta,
        }

        interrupt_id: Optional[str] = None
        last_text = ""
        last_stage: Optional[str] = None
        async for event in runner.run_async(
            user_id=channel_id,
            session_id=session_id,
            new_message=types.Content(
                role="user", parts=[types.Part(text="process this document")]
            ),
            state_delta=state_delta,
        ):
            # Drive the live status off the real event stream: each node tags its
            # events with node_info.path → friendly stage label. Only edit on an
            # actual stage change to avoid redundant chat_update calls.
            stage = event_stage_label(event)
            if stage is not None and stage != last_stage:
                last_stage = stage
                _update_status(slack_client, channel_id, status_ts, stage)
            iid = find_interrupt_id(event)
            if iid is not None:
                interrupt_id = iid
            text = extract_final_text(event)
            if text:
                last_text = text

        if interrupt_id is not None:
            _update_status(
                slack_client, channel_id, status_ts, "⏳ Needs your review"
            )
            summary = await _read_interrupt_summary(
                runner, app_name, channel_id, session_id, last_text
            )
            # Enrich the card with a per-document label so a user dropping
            # many files can tell the review cards apart. Read the paused
            # session state (the same fetch that backs the summary helper)
            # to derive filename + first invoice vendor / total.
            paused_state = await _read_session_state(
                runner, app_name,
                {"user_id": channel_id, "session_id": session_id},
            )
            doc_label = _doc_label_from_state(paused_state)
            posted = _post_approval_card(
                slack_client, channel_id, summary, interrupt_id,
                thread_ts=thread_ts, doc_label=doc_label,
            )
            write_interrupt(
                db,
                interrupt_id,
                session_id=session_id,
                channel_id=channel_id,
                slack_file_id=file_id,
                message_ts=posted,
                user_id=channel_id,
                extra={"summary": summary, "doc_label": doc_label},
            )
            return {"status": "paused", "op_id": interrupt_id, "message_ts": posted}

        append_result = await persist_and_deliver(
            runner=runner,
            ledger_store=ledger_store,
            slack_client=slack_client,
            channel_id=channel_id,
            session_id=session_id,
            app_name=app_name,
            user_id=channel_id,
            thread_ts=thread_ts,
        )

        if append_result.get("all_deduped"):
            _update_status(slack_client, channel_id, status_ts, "📋 Already recorded")
            _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
            _add_reaction(slack_client, channel_id, upload_msg_ts, "ballot_box_with_check")
            return {"status": "duplicate", "append": append_result}

        # Final state: collapse the evolving status to a terminal ✅. The full
        # delivery summary is posted by persist_and_deliver, so keep this short
        # to avoid double-posting the same detail.
        _update_status(slack_client, channel_id, status_ts, "✅ Processed")
        # Swap the 👀 reaction for ✅ on the user's original upload message.
        _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
        _add_reaction(slack_client, channel_id, upload_msg_ts, "white_check_mark")
        return {"status": "delivered", "append": append_result}


async def _ensure_session(
    runner: Any, app_name: str, user_id: str, session_id: Optional[str] = None
) -> None:
    """Create the session if it does not exist yet (idempotent, race-safe).

    Avoids a check-then-create TOCTOU race: attempt ``create_session`` directly
    and treat an already-exists error as success (a concurrent drop won the race).
    ``session_id`` defaults to ``user_id`` for the single-id (Q&A) caller.
    """
    if session_id is None:
        session_id = user_id
    from google.adk.errors.already_exists_error import AlreadyExistsError

    try:
        await runner.session_service.create_session(
            app_name=app_name, user_id=user_id, session_id=session_id, state={}
        )
    except AlreadyExistsError:
        # A concurrent create won the race; the existing session is fine to reuse.
        pass


async def _read_interrupt_summary(
    runner: Any, app_name: str, user_id: str, session_id: str, fallback: str
) -> str:
    """Best-effort: read the gate's approval summary off the paused session state."""
    try:
        session = await runner.session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        if session:
            msg = session.state.get("approval_message")
            if msg:
                return msg
    except Exception:  # noqa: BLE001 - summary is cosmetic
        pass
    return fallback or "This document needs your review before it is added to the ledger."


async def _read_session_state(
    runner: Any, app_name: str, interrupt: dict
) -> dict:
    """Best-effort: return the paused session's state dict (empty if unavailable).

    Reads the per-document session identified by the HITL interrupt correlation
    doc (its ``user_id`` / ``session_id``). Used at the HITL pause site to
    enrich the approval card with the uploaded document's identity, AND by the
    Edit-modal opener to pre-fill the per-line account / tax / amount fields.
    Returns an empty dict on any failure so callers can fall back gracefully
    — both call sites are best-effort.
    """
    user_id = interrupt.get("user_id") or interrupt.get("session_id")
    session_id = interrupt.get("session_id")
    if not session_id or not user_id:
        return {}
    try:
        session = await runner.session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        if session and getattr(session, "state", None):
            return dict(session.state)
    except Exception:  # noqa: BLE001 - best-effort reader
        logger.debug("get_session failed for %s/%s; falling back to empty state",
                     user_id, session_id)
    return {}


def _vendor_from_inv_dict(first: dict) -> Optional[str]:
    """Canonical vendor name from a serialized NormalizedInvoice dict.

    Mirrors ``NormalizedInvoice.counterparty``: ``supplier.name`` for purchases,
    ``customer.name`` for sales. The serialized shape (``asdict``) nests the
    parties, so the legacy flat ``vendor_name`` / ``issuer_name`` keys never
    exist on real state — they remain only as a defensive fallback.
    """
    if not isinstance(first, dict):
        return None
    doc_type = first.get("doc_type") or "purchase"
    party = first.get("supplier") if doc_type == "purchase" else first.get("customer")
    name = (party or {}).get("name") if isinstance(party, dict) else None
    return name or first.get("vendor_name") or first.get("issuer_name")


def _doc_label_from_state(state: dict) -> str:
    """Human label tying a review card to its uploaded document.

    Builds a one-line string that lets a user pick the right card out of N
    concurrent drops. Format:
    ``📄 <filename>[ · <vendor>][ · <CUR> <total>]``
    Vendor is taken from the first normalized invoice's ``vendor_name`` (with
    ``issuer_name`` as the sales-direction alias); total is shown only when
    it's a real number. When the state is empty (no invoice yet, or the
    session fetch failed) the label degrades to ``📄 document``.
    """
    fname = (state or {}).get("source_filename") or "document"
    invs = (state or {}).get(nodes.NORMALIZED_KEY) or []
    if invs:
        first = invs[0] if isinstance(invs[0], dict) else {}
        vendor = first.get("vendor_name") or first.get("issuer_name") or ""
        total = first.get("total_amount")
        cur = first.get("currency") or ""
        money = f" · {cur} {total:,.2f}" if isinstance(total, (int, float)) else ""
        vend = f" · {vendor}" if vendor else ""
        return f"📄 {fname}{vend}{money}"
    return f"📄 {fname}"


def _persist_corrections(client_store, state: dict, edits: dict) -> None:
    """Persist each account/tax edit as a per-client vendor Correction (ADR-0004).

    Write a Correction keyed by the invoice's vendor ONLY for fields the human
    actually CHANGED — i.e. where the submitted ``account_code`` / ``tax_code``
    differs from the value the pipeline proposed for that line (read from the
    normalized invoice's ``lines[index]`` in state: ``account_code`` and the
    canonical ``tax_treatment``). The Block-Kit modal re-submits EVERY line's
    current selection, changed or not; without this diff a multi-line invoice
    would write one Correction per line — all under the same vendor — and the
    last (unchanged) line would clobber the line the user really edited. Lines
    whose only change was ``amount`` (a one-off variance, not a vendor rule) are
    skipped. Reads the canonical vendor the SAME way the categorizer resolves
    it: the counterparty party name — ``supplier.name`` for purchases,
    ``customer.name`` for sales (mirrors ``NormalizedInvoice.counterparty``).
    The serialized invoice (``_inv_to_dict`` = ``asdict``) carries these as
    nested party dicts, never a flat ``vendor_name`` key — reading the flat key
    silently dropped every correction. Legacy flat keys are kept as a defensive
    fallback. No-ops cleanly when ``client_id`` is missing or the invoice list
    is empty so callers never crash on partial state.
    """
    client_id = state.get("client_id") if isinstance(state, dict) else None
    invs = (state.get(nodes.NORMALIZED_KEY) or []) if isinstance(state, dict) else []
    if not client_id or not invs:
        return
    first = invs[0] if isinstance(invs[0], dict) else {}
    vendor = _vendor_from_inv_dict(first)
    if not vendor:
        return
    proposed_lines = first.get("lines") or []
    for e in (edits.get("lines") or []):
        if not isinstance(e, dict):
            continue
        idx = e.get("index")
        prop = proposed_lines[idx] if isinstance(idx, int) and 0 <= idx < len(proposed_lines) else {}
        if not isinstance(prop, dict):
            prop = {}
        acct = e.get("account_code")
        # Edit DTO uses canonical InvoiceLine key ``tax_treatment`` (post-2026-06-15
        # rename — see nodes.EDITABLE_LINE_FIELDS). The ``add_correction`` API
        # keeps ``tax_code`` because that's the entity_memory schema field for
        # the vendor's learned-default tax category — a different concept from
        # one line's tax treatment.
        tax = e.get("tax_treatment")
        acct_changed = bool(acct) and acct != prop.get("account_code")
        tax_changed = bool(tax) and tax != prop.get("tax_treatment")
        if acct_changed or tax_changed:
            client_store.add_correction(
                client_id=client_id,
                vendor=vendor,
                account_code=acct if acct_changed else None,
                tax_code=tax if tax_changed else None,
            )


def _edits_from_view_state(view: dict) -> dict:
    """Convert a ``view_submission`` state into the line-edits dict.

    Maps each ``block_id`` of the form ``<prefix>_<i>`` (where ``<prefix>`` is
    ``acct`` / ``tax`` / ``amt``) into a field on the edits line at index ``i``.
    Lines are returned in ascending index order. The shape matches what
    ``apply_decision_node`` expects in ``ApproveDecision.edits["lines"]``.
    """
    values = (view.get("state") or {}).get("values") or {}
    by_index: dict[int, dict] = {}
    for block_id, payload in values.items():
        prefix, _, idx_s = block_id.partition("_")
        if not idx_s.isdigit():
            continue
        i = int(idx_s)
        el = payload.get("v") or {}
        if prefix == "acct" and el.get("selected_option"):
            by_index.setdefault(i, {})["account_code"] = el["selected_option"]["value"]
        elif prefix == "tax" and el.get("selected_option"):
            # Canonical InvoiceLine field name — see nodes.EDITABLE_LINE_FIELDS.
            by_index.setdefault(i, {})["tax_treatment"] = el["selected_option"]["value"]
        elif prefix == "amt" and el.get("value"):
            # Canonical InvoiceLine field name — see nodes.EDITABLE_LINE_FIELDS.
            by_index.setdefault(i, {})["net_amount"] = float(el["value"])
    lines = [{"index": i, **fields} for i, fields in sorted(by_index.items())]
    return {"lines": lines}


def _post_approval_card(
    slack_client: Any, channel_id: str, summary: str, op_id: str, thread_ts=None,
    doc_label: Optional[str] = None,
) -> Optional[str]:
    kwargs = {
        "channel": channel_id,
        "blocks": approval_card_blocks(summary, op_id, doc_label=doc_label),
        "text": "Review needed before adding to the ledger.",
    }
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = slack_client.chat_postMessage(**kwargs)
    data = resp.data if hasattr(resp, "data") else resp
    if isinstance(data, dict):
        return data.get("ts")
    return None


# --------------------------------------------------------------------------- #
# Approve / Edit / Reject action → resume the paused workflow
# --------------------------------------------------------------------------- #


async def handle_approval_action(
    *,
    runner: Any,
    ledger_store: SlackLedgerStore,
    db: Any,
    slack_client: Any,
    op_id: str,
    decision: str,
    app_name: str,
    edits: Optional[dict] = None,
) -> dict:
    """Resume a paused workflow with the human decision, then persist + deliver.

    Idempotent: ``resume_session`` is guarded by the ``processed/{op_id}`` marker
    so a double-click resumes (and therefore appends to the ledger) at most once.
    Updates the original Slack card to show the outcome.
    """
    if is_processed(db, op_id):
        logger.info("approval action for %s already processed; ignoring.", op_id)
        return {"status": "already_processed", "op_id": op_id}

    interrupt = read_interrupt(db, op_id)
    if interrupt is None:
        logger.warning("no interrupt doc for op_id %s; cannot resume.", op_id)
        return {"status": "missing_interrupt", "op_id": op_id}

    channel_id = interrupt["channel_id"]
    session_id = interrupt["session_id"]
    # user_id is the ADK user the per-doc session is stored under (channel_id by
    # convention). Older interrupt docs omit it → fall back to session_id.
    user_id = interrupt.get("user_id") or session_id
    summary = interrupt.get("summary") or ""

    events = await resume_session(
        runner, db, op_id, ApproveDecision(decision=decision, edits=edits)
    )

    append_result: dict = {}
    if decision != "reject":
        append_result = await persist_and_deliver(
            runner=runner,
            ledger_store=ledger_store,
            slack_client=slack_client,
            channel_id=channel_id,
            session_id=session_id,
            app_name=app_name,
            user_id=user_id,
        )
    else:
        update_interrupt_status(db, op_id, "rejected")
        _post_message(slack_client, channel_id, "Document rejected — nothing was added to the ledger.")

    _update_card(slack_client, interrupt, summary, decision)
    return {"status": "resumed", "op_id": op_id, "events": len(events), "append": append_result}


def _update_card(slack_client: Any, interrupt: dict, summary: str, decision: str) -> None:
    ts = interrupt.get("message_ts")
    channel_id = interrupt.get("channel_id")
    if not ts or not channel_id:
        return
    try:
        slack_client.chat_update(
            channel=channel_id,
            ts=ts,
            blocks=approval_outcome_blocks(summary, decision),
            text=f"Document {decision}d.",
        )
    except Exception:  # noqa: BLE001 - card update is cosmetic
        logger.exception("failed to update approval card for %s", channel_id)


# --------------------------------------------------------------------------- #
# Text-question → Q&A path
# --------------------------------------------------------------------------- #


async def answer_question(
    *,
    runner: Any,
    ledger_store: SlackLedgerStore,
    slack_client: Any,
    channel_id: str,
    question: str,
    app_name: str,
    client_store=None,
    message_ts: Optional[str] = None,
    thread_ts: Optional[str] = None,
    raw_thread_ts: Optional[str] = None,
) -> dict:
    """Run the chat ``assistant_agent`` against the client's ledger state.

    Steps:
    1. Resolve the client profile and latest FY from Firestore so ``read_rows``
       targets the correct workbook (instead of falling back to "unknown").
    2. Fetch ledger rows via :meth:`SlackLedgerStore.read_rows` and inject them
       into ``state["ledger_data"]`` via ``state_delta`` (also seeds the
       profile keys so ``assistant_instruction`` can name the client).
    3. Run the standalone chat ``runner`` (built via :func:`build_chat_runner`)
       — the assistant is a root LlmAgent and sees session history (multi-turn).
    4. Capture the final text from ``extract_final_text`` and post it.

    Concurrency: the chat session id is per-thread (or per-UTC-day for
    top-level messages) via :func:`_chat_session_id` so multi-turn history
    accumulates instead of being thrown away per question. ``user_id`` stays
    the ``channel_id``. The body runs under the same module-level semaphore
    as document processing.

    ``runner`` MUST be the chat runner (see :func:`build_chat_runner`); the
    document coordinator graph no longer carries text traffic (ADR-0008).

    Returns a small status dict ``{"status": "answered", "text": ...}``.
    """
    # Per-thread chat session id (ADR-0008): same thread → same session, so the
    # assistant sees the full multi-turn history. Day-bucket fallback for
    # top-level messages keeps a series of channel-level questions coherent.
    session_id = _chat_session_id(channel_id, raw_thread_ts, message_ts)

    async with _SEM:
        await _ensure_session(runner, app_name, channel_id, session_id)

        # Resolve client profile so we have client_id + fye_month.
        profile_delta = _profile_state_delta(client_store, channel_id) if client_store else {}
        client_id = profile_delta.get("client_id") or channel_id
        fye_month = profile_delta.get("fye_month")

        # Pick the best FY: latest pointer with data, else current FY from today.
        fy: str = "unknown"
        latest = await asyncio.to_thread(ledger_store.latest_fy, client_id)
        if latest:
            fy = latest
        elif fye_month:
            from datetime import date as _date
            from invoice_processing.export.fy import fy_for_date
            fy = str(fy_for_date(_date.today(), int(fye_month)))

        # Fetch ledger rows (returns [] if no workbook exists yet).
        try:
            ledger_rows = await asyncio.to_thread(
                ledger_store.read_rows,
                client_id=client_id,
                fy=fy,
                slack_client=slack_client,
                channel_id=channel_id,
            )
        except Exception:  # noqa: BLE001 — read failure is non-fatal; agent will say "not loaded"
            logger.exception("read_rows failed for channel %s fy %s", channel_id, fy)
            ledger_rows = []

        state_delta = {
            "channel_id": channel_id,
            **profile_delta,
            LEDGER_DATA_KEY: ledger_rows,
        }

        # Cap the model's per-turn context to the most recent 20 events so a
        # long chat thread does not grow unboundedly. ADK 2.2.0 API:
        # ``RunConfig(get_session_config=GetSessionConfig(num_recent_events=20))``
        # — see .venv/lib/python3.12/site-packages/google/adk/agents/run_config.py:330
        # (the ``get_session_config`` field is honored by the runner before each
        # turn). 20 is a starting cap; adjust after live QA.
        from google.adk.agents.run_config import RunConfig
        from google.adk.sessions.base_session_service import GetSessionConfig

        run_config = RunConfig(
            get_session_config=GetSessionConfig(num_recent_events=20)
        )

        answer_text = ""
        last_tool_text = ""
        async for event in runner.run_async(
            user_id=channel_id,
            session_id=session_id,
            new_message=types.Content(
                role="user", parts=[types.Part(text=question)]
            ),
            state_delta=state_delta,
            run_config=run_config,
        ):
            text = extract_final_text(event)
            if text:
                answer_text = text
            tool_text = extract_tool_response_text(event)
            if tool_text:
                last_tool_text = tool_text

    if not answer_text:
        # Safety net: model went silent after a tool call. Show the user the
        # tool's raw result — better than the opaque "rephrase" canned message.
        answer_text = last_tool_text or (
            "I couldn't find an answer. Please try rephrasing your question."
        )

    _post_message(slack_client, channel_id, answer_text, thread_ts=thread_ts)
    return {"status": "answered", "text": answer_text}


# --------------------------------------------------------------------------- #
# PDF download adapter (bytes, in-memory) reusing the parked SSRF-hardened helper
# --------------------------------------------------------------------------- #


def download_pdf_bytes(slack_client: Any, file_id: str) -> bytes:
    """Download a Slack file's bytes using the parked SSRF-hardened downloader.

    ``app.slack_app.slack_download_file`` streams to a temp dir (path-traversal +
    SSRF + size hardened); we read the bytes back and clean up immediately so no
    client PDF lingers on disk.
    """
    from app.slack_app import slack_download_file

    task_dir = tempfile.mkdtemp(prefix="ledgr_runner_")
    try:
        path = slack_download_file(slack_client, file_id, task_dir)
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        import shutil

        shutil.rmtree(task_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Bolt app + ADK runner construction (production wiring)
# --------------------------------------------------------------------------- #


def build_runner(*, session_service=None, artifact_service=None):
    """Construct the ADK ``Runner`` bound to the accounting App + Firestore sessions.

    Imports are deferred so importing this module never touches the network.
    """
    from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
    from google.adk.runners import Runner

    from accounting_agents.agent import app as adk_app
    from accounting_agents.sessions import FirestoreSessionService

    return Runner(
        app=adk_app,
        session_service=session_service or FirestoreSessionService(),
        artifact_service=artifact_service or InMemoryArtifactService(),
    )


def build_chat_runner(*, session_service=None, artifact_service=None):
    """Construct the ADK ``Runner`` bound to the standalone chat assistant App.

    Mirrors :func:`build_runner` but bound to ``assistant_app`` (the multi-turn
    root LlmAgent — see ADR-0008). Imports are deferred so importing this
    module never touches the network.
    """
    from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
    from google.adk.runners import Runner

    from accounting_agents.agent import assistant_app
    from accounting_agents.sessions import FirestoreSessionService

    return Runner(
        app=assistant_app,
        session_service=session_service or FirestoreSessionService(),
        artifact_service=artifact_service or InMemoryArtifactService(),
    )


def _setup_channel_id(body: dict) -> str:
    """Resolve the channel id from a button-action body (mirrors handle_setup_open)."""
    return (
        body.get("container", {}).get("channel_id")
        or body.get("channel", {}).get("id")
        or body.get("channel_id")
        or ""
    )


async def _derive_setup_prefill(slack_client: Any, body: dict) -> Optional[dict]:
    """Build the onboarding-modal prefill from the channel name (best-effort).

    The channel is already named after the client (e.g. ``akar-enterprises-pte-ltd``),
    so we look up its name via ``conversations_info`` and de-slugify it into a
    ``client_name`` prefill. Returns ``None`` when the name can't be resolved, so
    the modal simply opens empty (a lookup failure must not block setup).
    """
    channel_id = _setup_channel_id(body)
    if not channel_id:
        return None
    try:
        resp = await asyncio.to_thread(
            slack_client.conversations_info, channel=channel_id
        )
    except Exception:  # noqa: BLE001 - prefill is a convenience, never block setup
        logger.exception("conversations_info failed for %s", channel_id)
        return None
    data = resp.data if hasattr(resp, "data") else resp
    channel = data.get("channel") if isinstance(data, dict) else None
    raw_name = channel.get("name") if isinstance(channel, dict) else None
    client_name = deslugify_channel_name(raw_name or "")
    if not client_name:
        return None
    return {"client_name": client_name}


def build_async_app(
    *,
    runner,
    ledger_store: SlackLedgerStore,
    db: Any,
    store=None,
    bot_token: Optional[str] = None,
    chat_runner=None,
):
    """Build the Bolt ``AsyncApp`` wired to the document + HITL + chat handlers.

    Onboarding (``member_joined_channel`` / ``/ledgr`` + settings modal) reuses
    the parked synchronous handlers from ``app.slack_app`` via thread offload.

    Two runners:
    - ``runner`` drives the document coordinator graph (file uploads → HITL).
    - ``chat_runner`` drives the standalone chat assistant (text questions →
      multi-turn). Auto-built via :func:`build_chat_runner` if not supplied.

    ``store`` defaults to :class:`FirestoreClientStore` so onboarding writes
    end up in the SAME Firestore the pipeline reads — keeps the socket-mode
    path consistent with the FastAPI path (fixes ADR/notes: "socket-mode
    onboarding writes to InMemoryClientStore" gap).
    """
    from slack_bolt.async_app import AsyncApp

    from app.slack_app import (
        handle_ledgr_command,
        handle_member_joined,
        handle_onboarding_submit,
        handle_setup_open,
        handle_use_standard_coa,
    )

    if store is None:
        from invoice_processing.export.client_context import FirestoreClientStore
        store = FirestoreClientStore()

    if chat_runner is None:
        chat_runner = build_chat_runner()

    app_name = runner.app_name
    token = bot_token or os.environ.get("SLACK_BOT_TOKEN")
    async_app = AsyncApp(token=token)

    # Bolt hands async handlers an AsyncWebClient, but ALL our downstream Slack Web
    # API calls (files_info, chat_postMessage, reactions, files_upload_v2,
    # files_delete, conversations_info) + the parked sync handlers are written
    # synchronously. Use one sync WebClient (same bot token) for every Web API call;
    # the async `client` is only used for Bolt's `ack()`. This is why uploads
    # silently did nothing before — sync calls on the async client returned
    # un-awaited coroutines.
    from slack_sdk import WebClient as _SyncWebClient

    sync_client = _SyncWebClient(token=token)

    @async_app.event("file_shared")
    async def _file_shared(event, body, client):
        eid = body.get("event_id") or f"{event.get('type')}:{event.get('event_ts') or event.get('ts')}"
        if _seen.seen_before(eid):
            logger.debug("dedup: dropping duplicate file_shared event %s", eid)
            return
        file_id = event.get("file_id") or event.get("file", {}).get("id")
        channel_id = event.get("channel_id") or event.get("channel")
        if not file_id or not channel_id:
            return
        # File-level dedup: Slack sends BOTH file_shared and message/file_share for
        # one upload (distinct event_ids), so guard on the file id to process once.
        if _seen.seen_before(f"file:{file_id}"):
            logger.debug("dedup: file %s already being processed", file_id)
            return
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        _file_futures[file_id] = fut
        try:
            result = await process_file_event(
                runner=runner,
                ledger_store=ledger_store,
                db=db,
                slack_client=sync_client,
                channel_id=channel_id,
                file_id=file_id,
                app_name=app_name,
                download_fn=download_pdf_bytes,
                source_filename=_resolve_file_name(sync_client, file_id, event.get("file")),
            )
            fut.set_result(result)
        except Exception as exc:
            fut.set_exception(exc)
            raise

    async def _run_action(ack, body, client, decision):
        await ack()
        action = (body.get("actions") or [{}])[0]
        op_id = action.get("value")
        if not op_id:
            return
        await handle_approval_action(
            runner=runner,
            ledger_store=ledger_store,
            db=db,
            slack_client=sync_client,
            op_id=op_id,
            decision=decision,
            app_name=app_name,
        )

    @async_app.action("approve")
    async def _approve(ack, body, client):
        await _run_action(ack, body, client, "approve")

    @async_app.action("edit")
    async def _edit(ack, body, client):
        # The gate's RequestInput only accepts the schema-validated ApproveDecision;
        # per-line edits require a Block-Kit modal where the user can correct fields.
        # Edit opens a per-line modal pre-filled with the proposed extraction.
        # We must ack() immediately (<3s) and then synchronously call views_open
        # with the trigger_id (Slack invalidates it after a few seconds).
        await ack()
        op_id = (body.get("actions") or [{}])[0].get("value")
        if not op_id:
            return
        interrupt = read_interrupt(db, op_id)
        state = (
            await _read_session_state(runner, app_name, interrupt)
            if interrupt else {}
        )
        # Single-invoice assumption (Task 6's apply_decision_node logs a WARNING
        # when len(invs) > 1 — modal mirrors the same per-doc-session contract).
        invs = state.get(nodes.NORMALIZED_KEY) or [{}]
        lines = invs[0].get("lines") or []
        coa_options = [
            (c.get("code"), f"{c.get('code')} — {c.get('description')}")
            for c in (state.get("coa") or [])
        ]
        sync_client.views_open(
            trigger_id=body["trigger_id"],
            view=invoice_edit_modal(op_id, lines, coa_options),
        )

    @async_app.view("ledgr_invoice_edit")
    async def _edit_submit(ack, body, client):
        await ack()
        view = body["view"]
        # Empty op_id falls through to handle_approval_action which logs and no-ops
        # (matches the approve/reject convention in app/slack_app.py).
        op_id = view.get("private_metadata") or ""
        edits = _edits_from_view_state(view)
        # ADR-0004: capture the PRE-edit proposal BEFORE resuming. resume runs
        # apply_decision_node, which mutates ``invoices[0]['lines']`` in place to
        # the edited values — so reading state AFTER resume would make every line
        # look "unchanged" and persist nothing. We snapshot the paused session
        # state here (original proposed account/tax + vendor) and diff the edits
        # against it in _persist_corrections. Best-effort: a read failure must
        # never abort the handler.
        pre_state: dict = {}
        if op_id:
            try:
                interrupt = read_interrupt(db, op_id) or {}
                pre_state = await _read_session_state(runner, app_name, interrupt)
            except Exception:  # noqa: BLE001 - snapshot is best-effort
                logger.exception("failed to read pre-edit state for op_id %s", op_id)
                pre_state = {}
        await handle_approval_action(
            runner=runner, ledger_store=ledger_store, db=db, slack_client=sync_client,
            op_id=op_id, decision="edit", edits=edits, app_name=app_name,
        )
        # Persist each genuinely-changed account/tax edit as a per-client vendor
        # Correction so the next document from the same vendor auto-applies it.
        if op_id:
            try:
                _persist_corrections(_DEFAULT_CLIENT_STORE, pre_state, edits)
            except Exception:  # noqa: BLE001 - persistence is best-effort
                logger.exception(
                    "failed to persist corrections for op_id %s", op_id,
                )

    @async_app.action("reject")
    async def _reject(ack, body, client):
        await _run_action(ack, body, client, "reject")

    # --- onboarding + commands (reuse parked sync handlers off-thread) ---

    @async_app.action("ledgr_setup_open")
    async def _setup_open(body, ack, client):
        await ack()
        prefill = await _derive_setup_prefill(sync_client, body)
        await asyncio.to_thread(
            handle_setup_open, body, lambda *a, **k: None, sync_client, prefill
        )

    @async_app.action("ledgr_use_standard_coa")
    async def _use_standard(body, ack, client):
        await ack()
        await asyncio.to_thread(
            handle_use_standard_coa, body, lambda *a, **k: None, sync_client, store
        )

    @async_app.view("ledgr_onboarding")
    async def _onboarding(body, ack, client):
        await ack()
        await asyncio.to_thread(
            handle_onboarding_submit,
            body,
            lambda *a, **k: None,
            sync_client,
            store,
            lambda: "client-" + os.urandom(6).hex(),
        )

    @async_app.command("/ledgr")
    async def _ledgr(ack, body, client):
        await ack()
        await asyncio.to_thread(
            handle_ledgr_command, lambda *a, **k: None, body, sync_client, store
        )

    @async_app.event("member_joined_channel")
    async def _member_joined(event, body, context, client):
        eid = body.get("event_id") or f"{event.get('type')}:{event.get('event_ts') or event.get('ts')}"
        if _seen.seen_before(eid):
            logger.debug("dedup: dropping duplicate member_joined_channel event %s", eid)
            return
        bot_user_id = context.get("bot_user_id") or ""
        await asyncio.to_thread(handle_member_joined, body, None, sync_client, bot_user_id)

    # --- text-question + file-upload handler ---

    @async_app.event("message")
    async def _message(event, body, client):
        # Dedup: Slack socket-mode can redeliver the same event on reconnect.
        # One guard per message event_id covers both the file and text paths so
        # a redelivery of a file_share message is suppressed exactly once.
        eid = body.get("event_id") or f"{event.get('type')}:{event.get('ts')}"
        if _seen.seen_before(eid):
            logger.debug("dedup: dropping duplicate message event %s", eid)
            return

        # Ignore bot messages and edit/delete noise regardless of subtype.
        subtype = event.get("subtype") or ""
        if subtype in ("bot_message", "message_changed", "message_deleted"):
            return
        if event.get("bot_id"):
            return

        channel_id = event.get("channel")
        if not channel_id:
            return

        # File-upload path: message subtype "file_share" OR event carries a
        # "files" list (some Slack app configurations omit the subtype but still
        # include the files array).  Process each file independently; the shared
        # _seen guard above already prevents double-processing if the same
        # event_id is redelivered.
        files = event.get("files") or []
        if subtype == "file_share" or files:
            # ADR-0007: one Job summary message per batch drop, threaded.
            # Post the summary up-front (initial text), pass its ``ts`` as
            # ``thread_ts`` into each ``process_file_event`` so every per-doc
            # status / approval / delivery card lands under it, then edit the
            # summary in-place with the final tally once the loop finishes.
            from app.blocks import job_summary_text

            total = len(files)
            # Post the placeholder summary (top-level, no thread_ts).
            try:
                resp = sync_client.chat_postMessage(
                    channel=channel_id,
                    text=f"📥 Processing {total} document{'s' if total != 1 else ''}…",
                )
            except Exception:  # noqa: BLE001 - cosmetic; never abort the upload
                logger.exception("failed to post Job summary in %s", channel_id)
                resp = None
            summary_ts: Optional[str] = None
            if resp is not None:
                data = resp.data if hasattr(resp, "data") else resp
                if isinstance(data, dict):
                    summary_ts = data.get("ts")

            posted = 0
            needs_review = 0
            rejected = 0
            duplicates = 0
            software_hint = ""
            fy_hint = ""
            kind_hint = ""

            # COA-routing path A (ADR-0006): resolve once per batch whether
            # spreadsheets should be treated as COA uploads.  A channel with no
            # profile or status==pending_coa routes xlsx/csv to run_coa_ingest;
            # an active client's spreadsheets fall through to process_file_event
            # (they are treated as ordinary documents, e.g. bank statements).
            from app.slack_app import run_coa_ingest as _run_coa_ingest

            _resolved = store.get_by_channel(channel_id)
            _coa_pending = _resolved is None or getattr(_resolved, "status", None) == "pending_coa"

            def _say_in_channel(**kwargs):
                sync_client.chat_postMessage(channel=channel_id, **kwargs)

            for f in files:
                file_id = f.get("id") if isinstance(f, dict) else None
                if not file_id:
                    continue
                # File-level dedup: file_shared + message/file_share both fire for
                # one upload; guard on the file id so it's processed exactly once.
                if _seen.seen_before(f"file:{file_id}"):
                    logger.debug("dedup: file %s already being processed", file_id)
                    # Await the Future from the file_shared handler so the batch
                    # tally counts this file's outcome.
                    fut = _file_futures.pop(file_id, None)
                    if fut is not None:
                        try:
                            result = await fut
                        except Exception:
                            logger.debug("file_shared processing failed for %s", file_id)
                            result = None
                        status = (result or {}).get("status")
                        if status == "delivered":
                            posted += 1
                            append = (result or {}).get("append") or {}
                            if not software_hint and append.get("software"):
                                software_hint = str(append["software"])
                            if not fy_hint and append.get("fy"):
                                fy_hint = str(append["fy"])
                            if not kind_hint and append.get("kind"):
                                kind_hint = str(append["kind"])
                        elif status == "duplicate":
                            duplicates += 1
                        elif status == "paused":
                            needs_review += 1
                        elif status == "rejected_unreadable":
                            rejected += 1
                    continue
                logger.info(
                    "file upload received via message: file=%s channel=%s",
                    file_id, channel_id,
                )

                # COA-routing path A (ADR-0006): a spreadsheet dropped on a
                # not-yet-onboarded / pending_coa channel is a Chart-of-Accounts
                # upload, not a document to process.
                if _is_coa_upload(f, coa_pending=_coa_pending):
                    logger.info("routing spreadsheet %s to COA ingest for channel %s", file_id, channel_id)
                    import shutil as _shutil
                    import tempfile as _tempfile

                    from app.slack_app import slack_download_file as _dl
                    task_dir = _tempfile.mkdtemp(prefix="ledgr_coa_")
                    try:
                        local_path = await asyncio.to_thread(_dl, sync_client, file_id, task_dir)
                        await asyncio.to_thread(
                            _run_coa_ingest,
                            channel_id=channel_id,
                            file_path=local_path,
                            store=store,
                            say_fn=_say_in_channel,
                        )
                    finally:
                        _shutil.rmtree(task_dir, ignore_errors=True)
                    continue

                result = await process_file_event(
                    runner=runner,
                    ledger_store=ledger_store,
                    db=db,
                    slack_client=sync_client,
                    channel_id=channel_id,
                    file_id=file_id,
                    app_name=app_name,
                    download_fn=download_pdf_bytes,
                    thread_ts=summary_ts,
                    source_filename=_resolve_file_name(sync_client, file_id, f),
                )
                # Aggregate per-doc outcomes for the final tally edit.
                status = (result or {}).get("status")
                if status == "delivered":
                    posted += 1
                    # Borrow the first delivered doc's software + fy for the tally
                    # (mixed-FY drops are rare and ambiguous — first wins).
                    append = (result or {}).get("append") or {}
                    if not software_hint and append.get("software"):
                        software_hint = str(append["software"])
                    if not fy_hint and append.get("fy"):
                        fy_hint = str(append["fy"])
                    if not kind_hint and append.get("kind"):
                        kind_hint = str(append["kind"])
                elif status == "duplicate":
                    duplicates += 1
                elif status == "paused":
                    needs_review += 1
                elif status == "rejected_unreadable":
                    rejected += 1

            # Edit the summary in-place with the final tally (ADR-0007).
            if summary_ts:
                try:
                    final_text = job_summary_text(
                        total=total,
                        posted=posted,
                        needs_review=needs_review,
                        rejected=rejected,
                        duplicates=duplicates,
                        software=software_hint,
                        fy=fy_hint,
                        kind=kind_hint,
                    )
                    sync_client.chat_update(
                        channel=channel_id,
                        ts=summary_ts,
                        text=final_text,
                    )
                except Exception:  # noqa: BLE001 - cosmetic
                    logger.exception("failed to update Job summary in %s", channel_id)

            return

        # Text-question path: plain user message with no files.
        text = (event.get("text") or "").strip()
        if not text:
            return

        message_ts = event.get("ts")
        # RAW thread_ts: only set when the message is actually inside a Slack
        # thread. The chat session id keys on this so thread replies reuse the
        # same multi-turn session (ADR-0008). The REPLY destination keeps the
        # legacy fallback to message_ts so replies still land in the right place.
        raw_thread_ts = event.get("thread_ts")
        thread_ts = raw_thread_ts or message_ts
        logger.info(
            "question received via message: channel=%s ts=%s", channel_id, message_ts
        )
        await answer_question(
            runner=chat_runner,
            ledger_store=ledger_store,
            slack_client=sync_client,
            channel_id=channel_id,
            question=text,
            app_name=chat_runner.app_name,
            client_store=store,
            message_ts=message_ts,
            thread_ts=thread_ts,
            raw_thread_ts=raw_thread_ts,
        )

    return async_app


# --------------------------------------------------------------------------- #
# FastAPI / Cloud Run entrypoint (HTTP, multi-workspace OAuth)
# --------------------------------------------------------------------------- #


def build_fastapi_app():
    """Build a FastAPI app that delegates POST /slack/events to the ADK graph.

    Mirrors ``_main_async`` wiring (runner + db + ledger_store + build_async_app
    + FirestoreClientStore) but for the HTTP path used by Cloud Run production.
    Does NOT strip OAuth env vars (that is socket-mode only); production uses
    multi-workspace OAuth via Bolt's OAuthSettings.

    All network/store construction is LAZY (deferred to first request via
    _get_handler) so importing this module never touches the network.

    Route annotations use the module-level ``Request`` / ``Response`` names
    (imported at the top of this file) so FastAPI can resolve them even under
    ``from __future__ import annotations`` (PEP 563 stringifies all annotations;
    FastAPI resolves them against the module globals at decoration time).
    """
    from fastapi import FastAPI
    from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

    # All heavyweight objects are deferred to first request. Imports happen
    # inside _get_handler so that test patches applied to the source modules
    # (e.g. accounting_agents.sessions.FirestoreSessionService) are still active
    # at call time — the closure references the source by name, not a captured
    # value that was already resolved at build_fastapi_app() call time.
    _state: dict = {}

    def _get_handler():
        if "handler" not in _state:
            from accounting_agents.sessions import FirestoreSessionService
            from invoice_processing.export.client_context import FirestoreClientStore
            db = FirestoreSessionService().client
            runner = build_runner()
            ledger_store = SlackLedgerStore(db)
            async_app = build_async_app(
                runner=runner,
                ledger_store=ledger_store,
                db=db,
                store=FirestoreClientStore(),
            )
            _state["handler"] = AsyncSlackRequestHandler(async_app)
        return _state["handler"]

    api = FastAPI(title="Ledgr Slack Bot")

    @api.get("/healthz")
    async def healthz():
        import json
        from app.config import missing_slack_http, missing_slack_oauth
        http_missing = missing_slack_http()
        oauth_missing = missing_slack_oauth()
        if http_missing and oauth_missing:
            return Response(
                content=json.dumps({"ok": False, "missing": http_missing}),
                media_type="application/json",
                status_code=503,
            )
        return {"ok": True}

    @api.post("/slack/events")
    async def slack_events(req: Request):
        return await _get_handler().handle(req)

    return api


# --------------------------------------------------------------------------- #
# Socket-mode entrypoint (replaces root slack_bot.py)
# --------------------------------------------------------------------------- #


async def _main_async() -> None:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    from accounting_agents.sessions import FirestoreSessionService

    # Socket mode is the local/dev single-workspace path: authenticate with
    # SLACK_BOT_TOKEN directly. Bolt auto-enables the OAuth installation store
    # whenever SLACK_CLIENT_ID/SECRET are present in the environment (it checks
    # `is not None`, so even empty strings count), which would make it ignore the
    # bot token. Strip them here — AFTER all imports have run their .env loading —
    # so the AsyncApp built below uses the bot token. Multi-workspace OAuth is the
    # job of the FastAPI/Cloud Run entrypoint, not socket mode.
    for _k in ("SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET", "SLACK_OAUTH_STATE_SECRET"):
        os.environ.pop(_k, None)

    db = FirestoreSessionService().client
    runner = build_runner()
    ledger_store = SlackLedgerStore(db)
    # Onboarding/commands must write to the SAME Firestore the document pipeline
    # reads (_DEFAULT_CLIENT_STORE). Without this, build_async_app defaults to an
    # ephemeral InMemoryClientStore and socket-mode-registered profiles would be
    # invisible to processing (soft-gated as "no_profile").
    async_app = build_async_app(
        runner=runner, ledger_store=ledger_store, db=db,
        store=FirestoreClientStore(),
    )

    handler = AsyncSocketModeHandler(async_app, os.environ["SLACK_APP_TOKEN"])
    logger.info("Starting Ledgr ADK Slack runner in socket mode...")
    await handler.start_async()


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
