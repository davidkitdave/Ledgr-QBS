"""Feature-flagged Slack dispatch for the clean ``ledgr_agent`` tool path (Plan 6 / D.3)."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from accounting_agents import nodes
from ledgr_agent.slack.batch_to_ledger import ledger_payload_from_batch_result
from ledgr_agent.slack.hitl_bridge import (
    CLEAN_AGENT_HITL_KIND,
    approval_summary_from_batch,
    apply_edits_to_ledger_payload,
    op_id_for_file,
    should_pause_for_hitl,
)

logger = logging.getLogger(__name__)


def _credit_block_message(batch: dict[str, Any]) -> str:
    block_reason = str((batch.get("validation_summary") or {}).get("block_reason") or "zero_credit")
    remaining = (batch.get("credits") or {}).get("credits_remaining")
    if block_reason == "insufficient_credit":
        return (
            "You don't have enough credits to process this document. "
            f"Balance: {remaining if remaining is not None else 'unknown'}."
        )
    return (
        "You're out of credits — add more before dropping documents. "
        "Open the Ledgr app home tab to check your balance."
    )


async def pause_clean_agent_for_hitl(
    *,
    db: Any,
    runner: Any,
    slack_client: Any,
    channel_id: str,
    file_id: str,
    session_id: str,
    app_name: str,
    batch: dict[str, Any],
    payload: dict[str, Any],
    source_filename: str,
    profile_delta: dict[str, Any],
    thread_ts: Optional[str] = None,
    status_ts: Optional[str] = None,
    upload_msg_ts: Optional[str] = None,
    stage_state: Any = None,
    batch_mode: bool = False,
    status_callback: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Post the Approve/Edit/Reject card and persist a tool-native interrupt."""

    from accounting_agents.hitl import write_interrupt
    from accounting_agents.slack_runner import (
        _doc_label_from_state,
        _ensure_session,
        _apply_state_delta,
        _plan_status_blocks,
        _post_approval_card,
        _remove_reaction,
        _simple_status_blocks,
        _update_status,
    )

    summary = approval_summary_from_batch(batch)
    op_id = op_id_for_file(channel_id, file_id)
    doc_label = source_filename or str(batch.get("source_files") or ["document"])[0]

    await _ensure_session(runner, app_name, channel_id, session_id)
    await _apply_state_delta(
        runner,
        app_name,
        channel_id,
        session_id,
        {
            nodes.LEDGER_ROWS_KEY: payload,
            nodes.DELIVER_SUMMARY_KEY: nodes.compose_delivery_summary(payload),
            "approval_message": summary,
            "file_id": file_id,
            "source_filename": source_filename,
            "channel_id": channel_id,
            "delivered": False,
            **profile_delta,
        },
    )

    posted = _post_approval_card(
        slack_client,
        channel_id,
        summary,
        op_id,
        thread_ts=thread_ts,
        doc_label=_doc_label_from_state({"source_filename": source_filename}) or doc_label,
    )
    extra: dict[str, Any] = {
        "kind": CLEAN_AGENT_HITL_KIND,
        "summary": summary,
        "doc_label": doc_label,
        "ledger_payload": payload,
        "batch_result": batch,
    }
    if thread_ts:
        extra["thread_ts"] = thread_ts
    if status_ts:
        extra["status_ts"] = status_ts
    if source_filename:
        extra["source_filename"] = source_filename

    write_interrupt(
        db,
        op_id,
        session_id=session_id,
        channel_id=channel_id,
        slack_file_id=file_id,
        message_ts=posted,
        user_id=channel_id,
        extra=extra,
    )

    if stage_state is not None:
        stage_state.advance("commit")
        stage_state.set_output("policy", "Waiting for your approval")
    _update_status(
        slack_client,
        channel_id,
        status_ts,
        "⏳ Needs your review",
        blocks=_plan_status_blocks(stage_state, source_filename, channel_id)
        if stage_state is not None
        else _simple_status_blocks("⏳ Needs your review"),
    )
    _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
    if batch_mode and status_callback is not None:
        status_callback(
            {
                "file_label": source_filename,
                "stage": "Awaiting your review",
                "detail": "paused for approval",
                "status": "in_progress",
            }
        )
    return {
        "status": "paused",
        "op_id": op_id,
        "message_ts": posted,
        "channel_id": channel_id,
        "file_id": file_id,
        "batch": batch,
    }


async def handle_clean_agent_approval_action(
    *,
    runner: Any,
    ledger_store: Any,
    db: Any,
    slack_client: Any,
    op_id: str,
    decision: str,
    app_name: str,
    edits: Optional[dict] = None,
    client_store: Any = None,
) -> dict:
    """Resume a clean-agent HITL pause without an ADK graph ``RequestInput``."""

    from accounting_agents.hitl import (
        is_processed,
        mark_processed,
        read_interrupt,
        update_interrupt_status,
    )
    from accounting_agents.slack_runner import (
        _post_message,
        _record_familiarity_from_state,
        _update_card,
        _update_status,
        persist_and_deliver,
        _apply_state_delta,
        _plan_status_blocks,
        _StageState,
    )

    if is_processed(db, op_id):
        logger.info("clean-agent approval for %s already processed; ignoring.", op_id)
        return {"status": "already_processed", "op_id": op_id}

    interrupt = read_interrupt(db, op_id)
    if interrupt is None:
        logger.warning("no interrupt doc for op_id %s; cannot resume clean-agent batch.", op_id)
        return {"status": "missing_interrupt", "op_id": op_id}

    channel_id = interrupt["channel_id"]
    session_id = interrupt["session_id"]
    user_id = interrupt.get("user_id") or session_id
    summary = interrupt.get("summary") or ""
    thread_ts = interrupt.get("thread_ts") or None
    payload = dict(interrupt.get("ledger_payload") or {})
    status_ts = interrupt.get("status_ts")
    source_filename = interrupt.get("source_filename") or "document"

    mark_processed(db, op_id)

    append_result: dict = {}
    if decision == "reject":
        update_interrupt_status(db, op_id, "rejected")
        _post_message(
            slack_client,
            channel_id,
            "Document rejected — nothing was added to the ledger.",
            thread_ts=thread_ts,
        )
    else:
        if decision == "edit" and edits:
            payload = apply_edits_to_ledger_payload(payload, edits)
        payload["delivered"] = True
        from accounting_agents.slack_runner import _ensure_session

        await _ensure_session(runner, app_name, user_id, session_id)
        await _apply_state_delta(
            runner,
            app_name,
            user_id,
            session_id,
            {
                nodes.LEDGER_ROWS_KEY: payload,
                nodes.DELIVER_SUMMARY_KEY: nodes.compose_delivery_summary(payload),
                "delivered": True,
            },
        )
        append_result = await persist_and_deliver(
            runner=runner,
            ledger_store=ledger_store,
            slack_client=slack_client,
            channel_id=channel_id,
            session_id=session_id,
            app_name=app_name,
            user_id=user_id,
            thread_ts=thread_ts,
            client_store=client_store,
        )
        if decision == "approve":
            try:
                from accounting_agents.slack_runner import _read_session_state

                approved_state = await _read_session_state(
                    runner,
                    app_name,
                    {"user_id": user_id, "session_id": session_id},
                )
                if client_store is not None:
                    _record_familiarity_from_state(client_store, approved_state)
            except Exception:  # noqa: BLE001
                logger.debug("familiarity record failed after clean-agent approval", exc_info=True)

    _update_card(slack_client, interrupt, summary, decision)

    if status_ts:
        stage_state = _StageState()
        stage_state.mark_complete(
            output="Approved" if decision == "approve" else (
                "Rejected" if decision == "reject" else "Updated"
            ),
        )
        terminal = (
            "✅ Processed"
            if decision in {"approve", "edit"}
            else "❌ Rejected"
        )
        try:
            _update_status(
                slack_client,
                channel_id,
                status_ts,
                terminal,
                blocks=_plan_status_blocks(stage_state, source_filename, channel_id),
            )
        except Exception:  # noqa: BLE001
            logger.debug("failed to finalize plan status after clean-agent approval", exc_info=True)

    return {
        "status": "resumed",
        "op_id": op_id,
        "append": append_result,
        "decision": decision,
    }


def _tool_context_from_profile(profile_delta: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(state=dict(profile_delta))


async def process_file_via_clean_agent(
    *,
    runner: Any,
    ledger_store: Any,
    db: Any,
    slack_client: Any,
    channel_id: str,
    file_id: str,
    session_id: str,
    app_name: str,
    data: bytes,
    source_filename: str,
    profile_delta: dict[str, Any],
    client_store: Any,
    thread_ts: Optional[str] = None,
    status_ts: Optional[str] = None,
    upload_msg_ts: Optional[str] = None,
    replace: bool = False,
    defer_slack_delivery: bool = False,
    batch_mode: bool = False,
    defer_ledger_persist: bool = False,
    status_callback: Optional[Callable[[dict], None]] = None,
    stage_state: Any = None,
) -> dict:
    """Run ``process_document_batch`` and deliver through the existing Slack helpers."""

    from ledgr_agent.tools import process_document_batch

    from accounting_agents.slack_runner import (
        _add_reaction,
        _apply_state_delta,
        _ensure_session,
        _plan_status_blocks,
        _post_message,
        _remove_reaction,
        _simple_status_blocks,
        _terminal_status_line,
        _update_status,
        persist_and_deliver,
    )

    suffix = Path(source_filename).suffix or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        temp_path = tmp.name

    try:
        tool_context = _tool_context_from_profile(profile_delta)
        batch = process_document_batch(tool_context, paths=[temp_path])
    finally:
        Path(temp_path).unlink(missing_ok=True)

    status = str(batch.get("status") or "error")
    if status == "blocked":
        user_msg = _credit_block_message(batch)
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
        if batch_mode and status_callback is not None:
            status_callback(
                {
                    "file_label": source_filename,
                    "stage": "Out of credits",
                    "detail": user_msg,
                    "status": "failed",
                }
            )
        return {
            "status": "blocked",
            "channel_id": channel_id,
            "file_id": file_id,
            "batch": batch,
        }

    payload = ledger_payload_from_batch_result(
        batch,
        client_id=str(profile_delta.get("client_id") or batch.get("client_id") or "unknown"),
        client_name=str(profile_delta.get("client_name") or ""),
        software=str(profile_delta.get("software") or "QBS Ledger"),
        file_id=file_id,
        source_filename=source_filename,
        delivered=status == "success",
    )
    batches = payload.get("batches") or []

    if should_pause_for_hitl(batch):
        return await pause_clean_agent_for_hitl(
            db=db,
            runner=runner,
            slack_client=slack_client,
            channel_id=channel_id,
            file_id=file_id,
            session_id=session_id,
            app_name=app_name,
            batch=batch,
            payload=payload,
            source_filename=source_filename,
            profile_delta=profile_delta,
            thread_ts=thread_ts,
            status_ts=status_ts,
            upload_msg_ts=upload_msg_ts,
            stage_state=stage_state,
            batch_mode=batch_mode,
            status_callback=status_callback,
        )

    if status == "error" or not batches:
        detail = str((batch.get("validation_summary") or {}).get("engine_errors") or "processing failed")
        if isinstance(detail, list):
            detail = "; ".join(str(item) for item in detail) or "processing failed"
        if stage_state is not None:
            stage_state.mark_failed("understand", detail)
        _update_status(
            slack_client,
            channel_id,
            status_ts,
            "❌ Processing failed",
            blocks=_plan_status_blocks(stage_state, source_filename, channel_id)
            if stage_state is not None
            else _simple_status_blocks("❌ Processing failed"),
        )
        _post_message(
            slack_client,
            channel_id,
            f"Sorry, processing failed for `{source_filename}`: {detail}",
            thread_ts=thread_ts,
        )
        _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
        _add_reaction(slack_client, channel_id, upload_msg_ts, "x")
        if batch_mode and status_callback is not None:
            status_callback(
                {
                    "file_label": source_filename,
                    "stage": "Processing failed",
                    "detail": detail,
                    "status": "failed",
                }
            )
        return {
            "status": "processing_failed",
            "channel_id": channel_id,
            "file_id": file_id,
            "batch": batch,
            "error": detail,
        }

    await _ensure_session(runner, app_name, channel_id, session_id)
    await _apply_state_delta(
        runner,
        app_name,
        channel_id,
        session_id,
        {
            nodes.LEDGER_ROWS_KEY: payload,
            nodes.DELIVER_SUMMARY_KEY: nodes.compose_delivery_summary(payload),
            "file_id": file_id,
            "source_filename": source_filename,
            "delivered": status == "success",
            # Carry page count to delivery so page-based billing
            # (delivery_charge_units) can charge multi-page invoices/receipts by
            # pages, not just by captured-doc count.
            "input_page_count": profile_delta.get("input_page_count"),
        },
    )

    append_result = await persist_and_deliver(
        runner=runner,
        ledger_store=ledger_store,
        slack_client=slack_client,
        channel_id=channel_id,
        session_id=session_id,
        app_name=app_name,
        user_id=channel_id,
        thread_ts=thread_ts,
        replace=replace,
        defer_slack_delivery=defer_slack_delivery,
        batch_mode=batch_mode,
        defer_ledger_persist=defer_ledger_persist,
        client_store=client_store,
    )

    if append_result.get("all_deduped"):
        _update_status(
            slack_client,
            channel_id,
            status_ts,
            "📋 Already recorded",
            blocks=_simple_status_blocks("📋 Already recorded"),
        )
        _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
        _add_reaction(slack_client, channel_id, upload_msg_ts, "ballot_box_with_check")
        if batch_mode and status_callback is not None:
            status_callback(
                {
                    "file_label": source_filename,
                    "stage": "Already recorded",
                    "detail": "duplicate of a prior entry",
                    "status": "complete",
                }
            )
        return {"status": "duplicate", "append": append_result, "batch": batch}

    terminal = _terminal_status_line(append_result, payload)
    _update_status(
        slack_client,
        channel_id,
        status_ts,
        terminal,
        blocks=_simple_status_blocks(terminal),
    )
    _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
    _add_reaction(slack_client, channel_id, upload_msg_ts, "white_check_mark")
    if batch_mode and status_callback is not None:
        status_callback(
            {
                "file_label": source_filename,
                "stage": "Added to ledger",
                "detail": terminal,
                "status": "complete",
            }
        )
    return {
        "status": "delivered",
        "append": append_result,
        "batch": batch,
        "channel_id": channel_id,
        "file_id": file_id,
    }
