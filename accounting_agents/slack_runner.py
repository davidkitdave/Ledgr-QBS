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

Session convention: ``user_id == session_id == channel_id`` so the coordinator's
``before_agent_callback`` (which reads ``state["channel_id"]``) resolves the
client by channel.

The heavy lifting lives in module-level pure functions (``process_file_event`` /
``handle_approval_action`` / ``persist_and_deliver``) that take the runner,
store, Slack client, and Firestore db as explicit args, so they are unit-testable
with fakes and no live Slack/Gemini.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any, Optional

from accounting_agents.qa_agent import LEDGER_DATA_KEY

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
from app.blocks import approval_card_blocks, approval_outcome_blocks

logger = logging.getLogger(__name__)

#: Artifact filename convention shared with the graph (``nodes.ARTIFACT_NAME_FMT``).
ARTIFACT_NAME_FMT = nodes.ARTIFACT_NAME_FMT


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


def find_interrupt_id(event: Any) -> Optional[str]:
    """Return the interrupt id if ``event`` carries an ``adk_request_input`` call."""
    getter = getattr(event, "get_function_calls", None)
    calls = getter() if callable(getter) else []
    for fc in calls or []:
        if getattr(fc, "name", None) == REQUEST_INPUT_FUNCTION_CALL_NAME:
            return fc.id
    return None


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
    thread_ts: Optional[str] = None,
) -> dict:
    """Read the finished session's ledger payload → append to the FY workbook → post.

    Reads ``state["ledger_rows"]`` (prepared Slack-agnostically by
    ``consolidate_node``) from the persisted session and, when present, calls
    :meth:`SlackLedgerStore.append_rows` to fetch/append/re-upload the channel's
    FY workbook. Then posts the ``deliver_summary`` text. Returns the append
    result (or an empty dict when there was nothing to persist).
    """
    session = await runner.session_service.get_session(
        app_name=app_name, user_id=session_id, session_id=session_id
    )
    state = dict(session.state) if session else {}
    payload = state.get(nodes.LEDGER_ROWS_KEY) or {}
    batches = payload.get("batches") or []

    append_result: dict = {}
    if batches:
        append_result = ledger_store.append_rows(
            client_id=payload.get("client_id") or "unknown",
            fy=str(payload.get("fy") or "unknown"),
            slack_client=slack_client,
            channel_id=channel_id,
            batches=batches,
            software=payload.get("software") or "qbs",
            kind=payload.get("kind") or "invoice",
        )

    summary = state.get(nodes.DELIVER_SUMMARY_KEY) or "Document processed."
    _post_message(slack_client, channel_id, summary, thread_ts=thread_ts)
    return append_result


def _post_message(slack_client: Any, channel_id: str, text: str, thread_ts=None) -> None:
    kwargs = {"channel": channel_id, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    slack_client.chat_postMessage(**kwargs)


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
) -> dict:
    """Download the dropped PDF, run the workflow, and persist OR pause for HITL.

    Steps:
    1. ``download_fn(slack_client, file_id) -> bytes`` (the parked SSRF-hardened
       downloader, adapted to return bytes).
    2. ``save_artifact("inbox/{file_id}.pdf")`` into the runner's artifact service.
    3. ``runner.run_async(user_id=channel_id, session_id=channel_id, ...)`` with
       ``state_delta`` seeding ``channel_id`` / ``file_id`` / ``source_filename``
       / the artifact name.
    4. Stream events: if a HITL interrupt appears → ``write_interrupt`` + post the
       Approve/Edit/Reject card and STOP (workflow paused). Otherwise on normal
       completion → :func:`persist_and_deliver`.

    Returns a small status dict: ``{"status": "paused"|"delivered", ...}``.
    """
    data = download_fn(slack_client, file_id)
    artifact_name = ARTIFACT_NAME_FMT.format(file_id=file_id)

    await runner.artifact_service.save_artifact(
        app_name=app_name,
        user_id=channel_id,
        session_id=channel_id,
        filename=artifact_name,
        artifact=types.Part(
            inline_data=types.Blob(data=data, mime_type="application/pdf")
        ),
    )

    await _ensure_session(runner, app_name, channel_id)

    state_delta = {
        "channel_id": channel_id,
        "file_id": file_id,
        "source_filename": source_filename,
        nodes.ARTIFACT_NAME_KEY: artifact_name,
    }

    interrupt_id: Optional[str] = None
    last_text = ""
    async for event in runner.run_async(
        user_id=channel_id,
        session_id=channel_id,
        new_message=types.Content(
            role="user", parts=[types.Part(text="process this document")]
        ),
        state_delta=state_delta,
    ):
        iid = find_interrupt_id(event)
        if iid is not None:
            interrupt_id = iid
        text = extract_final_text(event)
        if text:
            last_text = text

    if interrupt_id is not None:
        summary = await _read_interrupt_summary(runner, app_name, channel_id, last_text)
        posted = _post_approval_card(slack_client, channel_id, summary, interrupt_id, thread_ts)
        write_interrupt(
            db,
            interrupt_id,
            session_id=channel_id,
            channel_id=channel_id,
            slack_file_id=file_id,
            message_ts=posted,
            extra={"summary": summary},
        )
        return {"status": "paused", "op_id": interrupt_id, "message_ts": posted}

    append_result = await persist_and_deliver(
        runner=runner,
        ledger_store=ledger_store,
        slack_client=slack_client,
        channel_id=channel_id,
        session_id=channel_id,
        app_name=app_name,
        thread_ts=thread_ts,
    )
    return {"status": "delivered", "append": append_result}


async def _ensure_session(runner: Any, app_name: str, channel_id: str) -> None:
    """Create the channel's session if it does not exist yet (idempotent)."""
    existing = await runner.session_service.get_session(
        app_name=app_name, user_id=channel_id, session_id=channel_id
    )
    if existing is None:
        await runner.session_service.create_session(
            app_name=app_name, user_id=channel_id, session_id=channel_id, state={}
        )


async def _read_interrupt_summary(
    runner: Any, app_name: str, channel_id: str, fallback: str
) -> str:
    """Best-effort: read the gate's approval summary off the paused session state."""
    try:
        session = await runner.session_service.get_session(
            app_name=app_name, user_id=channel_id, session_id=channel_id
        )
        if session:
            msg = session.state.get("approval_message")
            if msg:
                return msg
    except Exception:  # noqa: BLE001 - summary is cosmetic
        pass
    return fallback or "This document needs your review before it is added to the ledger."


def _post_approval_card(
    slack_client: Any, channel_id: str, summary: str, op_id: str, thread_ts=None
) -> Optional[str]:
    kwargs = {
        "channel": channel_id,
        "blocks": approval_card_blocks(summary, op_id),
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
    thread_ts: Optional[str] = None,
) -> dict:
    """Run the coordinator with a text question; qa_agent answers from ledger state.

    Steps:
    1. Resolve the client's current FY from session state (falls back to "unknown"
       so the ledger tool still returns a graceful "not loaded" message).
    2. Fetch ledger rows via :meth:`SlackLedgerStore.read_rows` and inject them
       into ``state["ledger_data"]`` via ``state_delta``.
    3. Run the coordinator via ``runner.run_async`` — the coordinator classifies
       "question" intent and routes to ``qa_agent``.
    4. Capture the final text from ``extract_final_text`` and post it to the channel.

    Returns a small status dict ``{"status": "answered", "text": ...}``.
    """
    await _ensure_session(runner, app_name, channel_id)

    # Best-effort: read FY from persisted session state so read_rows targets the
    # right workbook.  Falls back gracefully when not set.
    session = await runner.session_service.get_session(
        app_name=app_name, user_id=channel_id, session_id=channel_id
    )
    state_snapshot = dict(session.state) if session else {}
    client_id = state_snapshot.get("client_id") or channel_id
    fy = str(state_snapshot.get("fy") or state_snapshot.get("financial_year") or "unknown")

    # Fetch ledger rows (returns [] if no workbook exists yet).
    try:
        ledger_rows = ledger_store.read_rows(
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
        LEDGER_DATA_KEY: ledger_rows,
    }

    answer_text = ""
    async for event in runner.run_async(
        user_id=channel_id,
        session_id=channel_id,
        new_message=types.Content(
            role="user", parts=[types.Part(text=question)]
        ),
        state_delta=state_delta,
    ):
        text = extract_final_text(event)
        if text:
            answer_text = text

    if not answer_text:
        answer_text = "I couldn't find an answer. Please try rephrasing your question."

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


def build_async_app(
    *,
    runner,
    ledger_store: SlackLedgerStore,
    db: Any,
    store=None,
    bot_token: Optional[str] = None,
):
    """Build the Bolt ``AsyncApp`` wired to the document + HITL handlers.

    Onboarding (``member_joined_channel`` / ``/ledgr`` + settings modal) reuses
    the parked synchronous handlers from ``app.slack_app`` via thread offload.
    """
    from slack_bolt.async_app import AsyncApp

    from app.slack_app import (
        handle_ledgr_command,
        handle_member_joined,
        handle_onboarding_submit,
        handle_setup_open,
        handle_use_standard_coa,
    )
    from invoice_processing.export.client_context import InMemoryClientStore

    if store is None:
        store = InMemoryClientStore()

    app_name = runner.app_name
    token = bot_token or os.environ.get("SLACK_BOT_TOKEN")
    async_app = AsyncApp(token=token)

    @async_app.event("file_shared")
    async def _file_shared(event, body, client):
        file_id = event.get("file_id") or event.get("file", {}).get("id")
        channel_id = event.get("channel_id") or event.get("channel")
        if not file_id or not channel_id:
            return
        await process_file_event(
            runner=runner,
            ledger_store=ledger_store,
            db=db,
            slack_client=client,
            channel_id=channel_id,
            file_id=file_id,
            app_name=app_name,
            download_fn=download_pdf_bytes,
        )

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
            slack_client=client,
            op_id=op_id,
            decision=decision,
            app_name=app_name,
        )

    @async_app.action("approve")
    async def _approve(ack, body, client):
        await _run_action(ack, body, client, "approve")

    @async_app.action("edit")
    async def _edit(ack, body, client):
        await _run_action(ack, body, client, "edit")

    @async_app.action("reject")
    async def _reject(ack, body, client):
        await _run_action(ack, body, client, "reject")

    # --- onboarding + commands (reuse parked sync handlers off-thread) ---

    @async_app.action("ledgr_setup_open")
    async def _setup_open(body, ack, client):
        await ack()
        await asyncio.to_thread(handle_setup_open, body, lambda *a, **k: None, client)

    @async_app.action("ledgr_use_standard_coa")
    async def _use_standard(body, ack, client):
        await ack()
        await asyncio.to_thread(
            handle_use_standard_coa, body, lambda *a, **k: None, client, store
        )

    @async_app.view("ledgr_onboarding")
    async def _onboarding(body, ack, client):
        await ack()
        await asyncio.to_thread(
            handle_onboarding_submit,
            body,
            lambda *a, **k: None,
            client,
            store,
            lambda: "client-" + os.urandom(6).hex(),
        )

    @async_app.command("/ledgr")
    async def _ledgr(ack, body, client):
        await ack()
        await asyncio.to_thread(
            handle_ledgr_command, lambda *a, **k: None, body, client, store
        )

    @async_app.event("member_joined_channel")
    async def _member_joined(body, context, client):
        bot_user_id = context.get("bot_user_id") or ""
        await asyncio.to_thread(handle_member_joined, body, None, client, bot_user_id)

    # --- text-question handler (routes to qa_agent via the coordinator) ---

    @async_app.event("message")
    async def _message(event, client):
        # Ignore file_share subtypes (handled by file_shared above) and bot messages.
        subtype = event.get("subtype") or ""
        if subtype in ("file_share", "bot_message", "message_changed", "message_deleted"):
            return
        if event.get("bot_id"):
            return

        text = (event.get("text") or "").strip()
        if not text:
            return

        channel_id = event.get("channel")
        if not channel_id:
            return

        thread_ts = event.get("thread_ts") or event.get("ts")
        await answer_question(
            runner=runner,
            ledger_store=ledger_store,
            slack_client=client,
            channel_id=channel_id,
            question=text,
            app_name=app_name,
            thread_ts=thread_ts,
        )

    return async_app


# --------------------------------------------------------------------------- #
# Socket-mode entrypoint (replaces root slack_bot.py)
# --------------------------------------------------------------------------- #


async def _main_async() -> None:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    from accounting_agents.sessions import FirestoreSessionService

    db = FirestoreSessionService().client
    runner = build_runner()
    ledger_store = SlackLedgerStore(db)
    async_app = build_async_app(runner=runner, ledger_store=ledger_store, db=db)

    handler = AsyncSocketModeHandler(async_app, os.environ["SLACK_APP_TOKEN"])
    logger.info("Starting Ledgr ADK Slack runner in socket mode...")
    await handler.start_async()


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
