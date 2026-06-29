"""Thin Slack frontend for ledgr_agent (V1 — no HITL, no legacy graph)."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from types import SimpleNamespace
from typing import Any, Callable, Optional

from google.genai import types

from ledgr_agent.internal.uploads import artifact_name_for
from ledgr_slack.delivery import (
    compose_delivery_summary,
    workbook_from_session_state,
    workbook_to_ledger_payload,
)
from ledgr_slack.session import run_state_delta
from ledgr_agent.tools.build_sheets import WORKBOOK_STATE_KEY, build_sheets
from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY, read_doc

logger = logging.getLogger(__name__)


def build_ledgr_runner(*, session_service=None, artifact_service=None):
    """Construct ADK ``Runner`` bound to ``ledgr_app``."""
    from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
    from google.adk.runners import Runner

    from accounting_agents.credit_delivery import wire_shared_credit_service
    from accounting_agents.sessions import FirestoreSessionService
    from ledgr_agent.app import ledgr_app
    from ledgr_agent.billing import wire_playground_credits

    wire_shared_credit_service()
    wire_playground_credits()

    return Runner(
        app=ledgr_app,
        session_service=session_service or FirestoreSessionService(),
        artifact_service=artifact_service or InMemoryArtifactService(),
    )


async def _merge_session_state(
    runner: Any,
    app_name: str,
    user_id: str,
    session_id: str,
    state_delta: dict[str, Any],
) -> dict[str, Any]:
    from accounting_agents.slack_runner import _apply_state_delta, _ensure_session

    await _ensure_session(runner, app_name, user_id, session_id)
    await _apply_state_delta(runner, app_name, user_id, session_id, state_delta)
    session = await runner.session_service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )
    return dict(session.state) if session and getattr(session, "state", None) else dict(state_delta)


async def _run_ledgr_tools(
    runner: Any,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    state_delta: dict[str, Any],
    doc_path: Optional[str] = None,
) -> dict[str, Any]:
    """Run ``read_doc`` then ``build_sheets`` on the live session state.

    ``doc_path`` is an on-disk copy of the upload. The Slack path hands tools a
    bare state context (no ADK artifact access), so we read from disk rather than
    recover the saved artifact via ``ctx.load_artifact``.
    """
    from accounting_agents.slack_runner import _apply_state_delta, _ensure_session

    await _ensure_session(runner, app_name, user_id, session_id)
    await _apply_state_delta(runner, app_name, user_id, session_id, state_delta)
    session = await runner.session_service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )
    state = dict(session.state) if session and getattr(session, "state", None) else dict(state_delta)
    ctx = SimpleNamespace(state=state)
    read_out = read_doc(ctx, paths=[doc_path] if doc_path else [])
    status = str(read_out.get("status") or "error")
    if status != "success":
        return read_out

    build_out = build_sheets(ctx)
    await _apply_state_delta(
        runner,
        app_name,
        user_id,
        session_id,
        {
            READ_DOC_STATE_KEY: ctx.state.get(READ_DOC_STATE_KEY),
            WORKBOOK_STATE_KEY: ctx.state.get(WORKBOOK_STATE_KEY),
        },
    )
    return build_out


async def deliver_workbook(
    *,
    runner: Any,
    ledger_store: Any,
    slack_client: Any,
    channel_id: str,
    session_id: str,
    app_name: str,
    user_id: str,
    profile_delta: dict[str, Any],
    source_filename: str,
    file_id: str,
    thread_ts: Optional[str] = None,
    client_store: Any = None,
    defer_slack_delivery: bool = False,
) -> dict[str, Any]:
    """Append workbook rows to the FY ledger and post a summary delivery card.

    Also records a per-document ``processing_log`` entry (so the chat lane can
    introspect deliveries — Phase 1/2 thread-context). When
    ``defer_slack_delivery`` is set (batch mode), the rich card is NOT posted
    per-doc; the summary/batches/payload are stashed under
    ``append["deferred_delivery"]`` so the batch coordinator can post one
    aggregate card at batch end.
    """
    from accounting_agents.slack_runner import _post_delivery_card, _record_processing_log

    session = await runner.session_service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )
    state = dict(session.state) if session and getattr(session, "state", None) else {}
    workbook = workbook_from_session_state(state)
    if not workbook or workbook.get("status") != "success":
        return {"status": "error", "message": "no workbook to deliver"}

    payload = workbook_to_ledger_payload(
        workbook,
        client_id=str(profile_delta.get("client_id") or state.get("client_id") or "unknown"),
        client_name=str(profile_delta.get("client_name") or ""),
        software=str(profile_delta.get("software") or "qbs"),
        file_id=file_id,
        source_filename=source_filename,
    )
    batches = payload.get("batches") or []
    if not batches:
        return {"status": "error", "message": "workbook produced no rows"}

    append_result = await asyncio.to_thread(
        ledger_store.append_rows,
        client_id=payload["client_id"],
        fy=str(payload.get("fy") or "unknown"),
        slack_client=slack_client,
        channel_id=channel_id,
        batches=batches,
        software=payload.get("software") or "qbs",
        kind=payload.get("kind") or "invoice",
        client_name=payload.get("client_name") or "",
    )
    append_result.setdefault("kind", payload.get("kind") or "invoice")
    append_result.setdefault("software", payload.get("software") or "qbs")
    append_result.setdefault("fy", str(payload.get("fy") or ""))
    summary = compose_delivery_summary(workbook, payload)

    if defer_slack_delivery:
        # Batch mode: stash the delivery so the coordinator posts ONE aggregate
        # card at batch end; do not post a per-doc card here.
        append_result["deferred_delivery"] = {
            "summary": summary,
            "batches": batches,
            "payload": payload,
            "workbook_name": append_result.get("filename") or "",
        }
    else:
        _post_delivery_card(
            slack_client,
            channel_id,
            summary=summary,
            batches=batches,
            payload=payload,
            append_result=append_result,
            thread_ts=thread_ts,
        )

    # Per-document audit log so the chat lane can resolve thread replies back to
    # the specific delivery the user is asking about (delivery_message_ts links
    # the card's parent message; channel_id scopes the lookup).
    if client_store is not None and not append_result.get("all_deduped"):
        try:
            _record_processing_log(
                state=state,
                payload=payload,
                batches=batches,
                append_result=append_result,
                client_store=client_store,
                delivery_message_ts=thread_ts,
                channel_id=channel_id,
            )
        except Exception:  # noqa: BLE001 — audit log must never break delivery
            logger.warning("processing_log write failed (non-fatal)", exc_info=True)

    return {
        "status": "delivered",
        "summary": summary,
        "append": append_result,
        "payload": payload,
    }


async def process_file_via_ledgr_agent(
    *,
    runner: Any,
    ledger_store: Any,
    slack_client: Any,
    channel_id: str,
    file_id: str,
    session_id: str,
    app_name: str,
    data: bytes,
    source_filename: str,
    profile_delta: dict[str, Any],
    thread_ts: Optional[str] = None,
    status_ts: Optional[str] = None,
    upload_msg_ts: Optional[str] = None,
    input_page_count: int = 1,
    batch_mode: bool = False,
    status_callback: Optional[Callable[[dict], None]] = None,
    stage_state: Any = None,
    client_store: Any = None,
    defer_slack_delivery: bool = False,
) -> dict[str, Any]:
    """Download-free file bytes → ledgr tools → FY workbook delivery."""
    from accounting_agents.credit_delivery import credit_block_message
    from accounting_agents.slack_runner import (
        _add_reaction,
        _post_message,
        _remove_reaction,
        _simple_status_blocks,
        _update_status,
    )
    from ledgr_agent.internal.gemini import mime_for

    artifact_name = artifact_name_for(file_id)
    artifact_mime = mime_for(source_filename)
    if artifact_mime == "application/octet-stream":
        artifact_mime = "application/pdf"

    save_result = runner.artifact_service.save_artifact(
        app_name=app_name,
        user_id=channel_id,
        session_id=session_id,
        filename=artifact_name,
        artifact=types.Part(
            inline_data=types.Blob(data=data, mime_type=artifact_mime),
        ),
    )
    if asyncio.iscoroutine(save_result):
        await save_result

    state_delta = run_state_delta(
        channel_id=channel_id,
        file_id=file_id,
        source_filename=source_filename,
        artifact_name=artifact_name,
        profile_delta=profile_delta,
        input_page_count=input_page_count,
    )

    # Stage the bytes on disk so read_doc can read them directly: the lean Slack
    # shortcut hands tools a bare state context with no ADK artifact access.
    fd, doc_path = tempfile.mkstemp(
        prefix="ledgr_", suffix=os.path.splitext(source_filename)[1] or ".pdf"
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        tool_result = await _run_ledgr_tools(
            runner,
            app_name=app_name,
            user_id=channel_id,
            session_id=session_id,
            state_delta=state_delta,
            doc_path=doc_path,
        )
    finally:
        try:
            os.unlink(doc_path)
        except OSError:
            pass
    status = str(tool_result.get("status") or "error")

    if status == "blocked":
        credits = tool_result.get("credits") or {}
        block_reason = credits.get("block_reason") or "zero_credit"
        user_msg = credit_block_message({"reason": block_reason, "credits_remaining": credits.get("credits_remaining")})
        if stage_state is not None:
            stage_state.mark_failed("understand", "Out of credits")
        _update_status(
            slack_client,
            channel_id,
            status_ts,
            "❌ Out of credits",
            blocks=_simple_status_blocks("❌ Out of credits"),
        )
        _post_message(slack_client, channel_id, user_msg, thread_ts=thread_ts)
        _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
        _add_reaction(slack_client, channel_id, upload_msg_ts, "x")
        return {
            "status": "blocked",
            "channel_id": channel_id,
            "file_id": file_id,
            "tool_result": tool_result,
        }

    if status == "error":
        detail = str(tool_result.get("message") or "processing failed")
        if stage_state is not None:
            stage_state.mark_failed("understand", detail)
        _update_status(
            slack_client,
            channel_id,
            status_ts,
            "❌ Processing failed",
            blocks=_simple_status_blocks("❌ Processing failed"),
        )
        _post_message(
            slack_client,
            channel_id,
            f"Sorry, I couldn't process `{source_filename}` — {detail}.",
            thread_ts=thread_ts,
        )
        _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
        _add_reaction(slack_client, channel_id, upload_msg_ts, "x")
        return {
            "status": "error",
            "channel_id": channel_id,
            "file_id": file_id,
            "tool_result": tool_result,
        }

    delivery = await deliver_workbook(
        runner=runner,
        ledger_store=ledger_store,
        slack_client=slack_client,
        channel_id=channel_id,
        session_id=session_id,
        app_name=app_name,
        user_id=channel_id,
        profile_delta=profile_delta,
        source_filename=source_filename,
        file_id=file_id,
        thread_ts=thread_ts,
        client_store=client_store,
        defer_slack_delivery=defer_slack_delivery,
    )
    if stage_state is not None:
        stage_state.mark_complete(output="Delivered")
    _update_status(
        slack_client,
        channel_id,
        status_ts,
        "✅ Processed",
        blocks=_simple_status_blocks("✅ Processed"),
    )
    _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
    _add_reaction(slack_client, channel_id, upload_msg_ts, "white_check_mark")
    if batch_mode and status_callback is not None:
        status_callback(
            {
                "file_label": source_filename,
                "stage": "Delivered",
                "detail": delivery.get("summary"),
                "status": "complete",
            }
        )
    return {
        "status": "delivered",
        "channel_id": channel_id,
        "file_id": file_id,
        "delivery": delivery,
        # The batch coordinator reads result["append"]["deferred_delivery"] to
        # stash per-doc rows for a single aggregate card at batch end.
        "append": delivery.get("append") or {},
    }
