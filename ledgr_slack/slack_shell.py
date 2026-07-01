"""Thin Slack frontend for ledgr_agent (V1 — no HITL, no legacy graph)."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from types import SimpleNamespace
from typing import Any, Callable, Optional

from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.adk.runners import Runner
from google.genai import types

from ledgr_agent.app import ledgr_app
from ledgr_agent.billing import _charge_disabled, get_shared_credit_service, wire_playground_credits
from ledgr_agent.internal.gemini import mime_for
from ledgr_agent.internal.uploads import artifact_name_for
from ledgr_agent.tools.build_sheets import WORKBOOK_STATE_KEY, build_sheets
from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY, read_doc
from ledgr_slack.credit_adapter import (
    charge_delivery_credits,
    credit_block_message,
    resolve_firm_id_from_client,
    resolve_firm_id_from_state,
    wire_shared_credit_service,
)
from ledgr_slack.delivery import (
    compose_delivery_summary,
    ledger_replace_for_batches,
    workbook_from_session_state,
    workbook_to_ledger_payload,
)
from ledgr_slack.session import run_state_delta
from ledgr_slack.sessions import FirestoreSessionService, _apply_state_delta, _ensure_session
from ledgr_slack.ux import (
    _add_reaction,
    _fail_doc,
    _post_delivery_card,
    _record_processing_log,
    _remove_reaction,
    _simple_status_blocks,
    _update_status,
)

logger = logging.getLogger(__name__)


def _group_batches_by_fy(batches: list[dict[str, Any]], default_fy: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for batch in batches:
        fy = str(batch.get("fy") or default_fy or "unknown")
        groups.setdefault(fy, []).append(batch)
    return groups


async def _append_workbook_batches(
    *,
    ledger_store: Any,
    payload: dict[str, Any],
    batches: list[dict[str, Any]],
    slack_client: Any,
    channel_id: str,
) -> dict[str, Any]:
    """Append batches, splitting across FY workbooks when needed."""
    default_fy = str(payload.get("fy") or "unknown")
    fy_groups = _group_batches_by_fy(batches, default_fy)
    merged: dict[str, Any] = {"appended": 0, "deduped": 0, "fy_groups": []}
    for fy, fy_batches in fy_groups.items():
        result = await asyncio.to_thread(
            ledger_store.append_rows,
            client_id=payload["client_id"],
            fy=fy,
            slack_client=slack_client,
            channel_id=channel_id,
            batches=fy_batches,
            software=payload.get("software") or "qbs",
            kind=payload.get("kind") or "invoice",
            client_name=payload.get("client_name") or "",
        )
        merged["appended"] = int(merged.get("appended") or 0) + int(result.get("appended") or 0)
        merged["deduped"] = int(merged.get("deduped") or 0) + int(result.get("deduped") or 0)
        merged.setdefault("kind", payload.get("kind") or "invoice")
        merged.setdefault("software", payload.get("software") or "qbs")
        merged["fy"] = fy
        if result.get("filename"):
            merged["filename"] = result["filename"]
        if result.get("slack_file_id"):
            merged["slack_file_id"] = result["slack_file_id"]
        merged["fy_groups"].append(
            {
                "fy": fy,
                "filename": result.get("filename"),
                "slack_file_id": result.get("slack_file_id"),
                "appended": int(result.get("appended") or 0),
                "deduped": int(result.get("deduped") or 0),
                "n_docs": len(fy_batches),
                "n_rows": sum(len(b.get("rows") or []) for b in fy_batches),
            }
        )
    return merged


def build_ledgr_runner(*, session_service=None, artifact_service=None):
    """Construct ADK ``Runner`` bound to ``ledgr_app``."""
    wire_shared_credit_service()
    wire_playground_credits()

    return Runner(
        app=ledgr_app,
        session_service=session_service or FirestoreSessionService(),
        artifact_service=artifact_service or InMemoryArtifactService(),
    )


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
    await _ensure_session(runner, app_name, user_id, session_id)
    await _apply_state_delta(runner, app_name, user_id, session_id, state_delta)
    session = await runner.session_service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )
    state = dict(session.state) if session and getattr(session, "state", None) else dict(state_delta)
    ctx = SimpleNamespace(state=state)
    read_out = await asyncio.to_thread(read_doc, ctx, paths=[doc_path] if doc_path else [])
    status = str(read_out.get("status") or "error")
    if status != "success":
        return read_out

    build_out = await asyncio.to_thread(build_sheets, ctx)
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

    When ``defer_slack_delivery`` is set (batch mode), ledger writes and the rich
    card are deferred; payloads are stashed under ``append["deferred_ledger"]``
    and ``append["deferred_delivery"]`` for the batch coordinator.
    """
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

    effective_replace = ledger_replace_for_batches(
        ledger_store,
        client_id=payload["client_id"],
        fy=str(payload.get("fy") or "unknown"),
        batches=batches,
        kind=str(payload.get("kind") or "invoice"),
    )
    pre_summary = compose_delivery_summary(workbook, payload, append_result=None)
    summary = pre_summary

    if defer_slack_delivery:
        append_result: dict[str, Any] = {
            "deferred_ledger": {
                "summary": pre_summary,
                "batches": batches,
                "payload": payload,
                "effective_replace": effective_replace,
            },
            "kind": payload.get("kind") or "invoice",
            "software": payload.get("software") or "qbs",
            "fy": str(payload.get("fy") or ""),
            "appended": 0,
        }
        append_result["deferred_delivery"] = {
            "summary": pre_summary,
            "batches": batches,
            "payload": payload,
            "workbook_name": "",
            "effective_replace": effective_replace,
            "file_id": file_id,
            "input_page_count": state.get("input_page_count"),
            "credits": workbook.get("credits") or {},
        }
    else:
        append_result = await _append_workbook_batches(
            ledger_store=ledger_store,
            payload=payload,
            batches=batches,
            slack_client=slack_client,
            channel_id=channel_id,
        )
        if not append_result.get("all_deduped"):
            credits_block = workbook.get("credits") or {}
            should_charge = _charge_disabled() or (
                isinstance(credits_block, dict)
                and credits_block.get("credit_status") == "estimated"
            )
            if should_charge:
                firm_id = resolve_firm_id_from_state({**state, **profile_delta})
                if firm_id is None and client_store is not None:
                    try:
                        ctx = client_store.get_by_channel(channel_id)
                        firm_id = resolve_firm_id_from_client(ctx)
                    except Exception:  # noqa: BLE001 — billing must not break delivery
                        logger.warning(
                            "credit firm-id lookup failed for channel=%s (non-fatal)",
                            channel_id,
                            exc_info=True,
                        )
                        firm_id = None
                credit_info = charge_delivery_credits(
                    firm_id=firm_id,
                    channel_id=channel_id,
                    file_id=file_id,
                    kind=str(payload.get("kind") or "invoice"),
                    payload={**payload, "input_page_count": state.get("input_page_count")},
                    append_result=append_result,
                    input_page_count=state.get("input_page_count"),
                )
                if credit_info:
                    append_result.update(credit_info)
        else:
            firm_id = resolve_firm_id_from_state({**state, **profile_delta})
            if firm_id is None and client_store is not None:
                try:
                    ctx = client_store.get_by_channel(channel_id)
                    firm_id = resolve_firm_id_from_client(ctx)
                except Exception:  # noqa: BLE001
                    firm_id = None
            if firm_id:
                append_result["credits_used"] = 0
                append_result["credits_remaining"] = int(
                    get_shared_credit_service().read_balance(firm_id)
                )
                append_result["all_deduped"] = True
        summary = compose_delivery_summary(workbook, payload, append_result=append_result)
        _post_delivery_card(
            slack_client,
            channel_id,
            summary=summary,
            batches=batches,
            payload=payload,
            append_result=append_result,
            thread_ts=thread_ts,
        )

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
        defer_slack_delivery=defer_slack_delivery,
    )

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
        remaining = credits.get("credits_remaining")
        if (
            credits.get("credit_status") == "blocked"
            and isinstance(remaining, int)
            and remaining > 0
        ):
            block_reason = "insufficient_credit"
        else:
            block_reason = (
                "insufficient_credit"
                if isinstance(remaining, int) and remaining > 0
                else "zero_credit"
            )
        user_msg = credit_block_message({
            "reason": block_reason,
            "credits_remaining": remaining,
        })
        return _fail_doc(
            slack_client,
            channel_id,
            source_filename=source_filename,
            status_headline="❌ Out of credits",
            user_message=user_msg,
            return_status="blocked",
            stage_state=stage_state,
            stage_error="Out of credits",
            status_ts=status_ts,
            upload_msg_ts=upload_msg_ts,
            thread_ts=thread_ts,
            file_id=file_id,
            extra_return={"tool_result": tool_result},
        )

    if status == "error":
        detail = str(tool_result.get("message") or "processing failed")
        return _fail_doc(
            slack_client,
            channel_id,
            source_filename=source_filename,
            status_headline="❌ Processing failed",
            user_message=f"Sorry, I couldn't process `{source_filename}` — {detail}.",
            return_status="error",
            stage_state=stage_state,
            stage_error=detail,
            status_ts=status_ts,
            upload_msg_ts=upload_msg_ts,
            thread_ts=thread_ts,
            file_id=file_id,
            extra_return={"tool_result": tool_result},
        )

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
    if str(delivery.get("status") or "") != "delivered":
        detail = str(delivery.get("message") or "delivery failed")
        return _fail_doc(
            slack_client,
            channel_id,
            source_filename=source_filename,
            status_headline="❌ Delivery failed",
            user_message=f"Sorry, I couldn't deliver `{source_filename}` — {detail}.",
            return_status="error",
            stage_state=stage_state,
            stage_error=detail,
            status_ts=status_ts,
            upload_msg_ts=upload_msg_ts,
            thread_ts=thread_ts,
            file_id=file_id,
            extra_return={"tool_result": tool_result, "delivery": delivery},
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
        "append": delivery.get("append") or {},
    }
