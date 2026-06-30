"""Per-document Slack file upload → ledgr_agent processing."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from app.slack_app import slack_download_file
from ledgr_slack.client_context import FirestoreClientStore

from ledgr_slack.client_store import (
    _DEFAULT_CLIENT_STORE,
    _profile_state_delta,
)
from ledgr_slack.config import _env_prefix
from ledgr_slack.credit_adapter import (
    credit_block_message,
    credit_gate_for_bytes,
    estimate_upload_pages,
    flag_unresolved_firm_billing_anomaly,
    require_firm_for_billing,
    resolve_firm_id_from_state,
)
from ledgr_slack.ledger_store import SlackLedgerStore
from ledgr_slack.slack_shell import process_file_via_ledgr_agent
from ledgr_slack.ux import (
    _StageState,
    _add_reaction,
    _fail_doc,
    _post_message,
    _post_status,
    _remove_reaction,
    _resolve_file_message_ts,
)

logger = logging.getLogger(__name__)

ARTIFACT_NAME_FMT = "inbox/{file_id}.pdf"

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
    ext = Path(source_filename).suffix.lower()
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

def _per_doc_session_id(channel_id: str, file_id: str) -> str:
    """Unique session id per dropped document so concurrent drops never collide."""
    return f"{channel_id}:{file_id}"

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
    hint: str = "",
    replace: bool = False,
    defer_slack_delivery: bool = False,
    batch_mode: bool = False,
    defer_ledger_persist: bool = False,
    status_callback: Optional[Callable[[dict], None]] = None,
    profile_delta: Optional[dict] = None,
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
    if profile_delta is None:
        profile_delta = _profile_state_delta(client_store, channel_id)
    else:
        profile_delta = dict(profile_delta)
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
    # Initial post is a compact one-liner; stage updates swap in the plan accordion.
    # In batch_mode the job-summary message owns the per-batch UX — posting a
    # per-doc "Received" + plan accordion here would stampede the channel with
    # N top-level messages (the pre-1B bug). Suppress both, leave status_ts=None
    # so _update_status no-ops, and let HITL cards still thread under the job.
    _stage_state = _StageState()
    if batch_mode:
        status_ts = None
    else:
        status_ts = _post_status(
            slack_client,
            channel_id,
            f"{_env_prefix()}📥 Received `{source_filename}` — on it…",
            thread_ts,
        )

    async with _SEM:
        data = await asyncio.to_thread(download_fn, slack_client, file_id)

        # Validate before touching the graph: reject empty files and unsupported
        # extensions so garbage never reaches Gemini and is not counted as
        # "processed" in the batch-drop tally.
        rejection_reason = _validate_download(data, source_filename)
        if rejection_reason is not None:
            logger.warning(
                "rejected unreadable upload: file=%s channel=%s reason=%s",
                file_id, channel_id, rejection_reason,
            )
            return _fail_doc(
                slack_client,
                channel_id,
                source_filename=source_filename,
                status_headline="❌ Couldn't read this file",
                user_message=(
                    f"Sorry, I couldn't read `{source_filename}` — {rejection_reason}. "
                    "Please re-upload a supported document (PDF, PNG, JPG, WEBP, or GIF)."
                ),
                return_status="rejected_unreadable",
                stage_state=_stage_state,
                stage_error="Couldn't read this file",
                status_ts=status_ts,
                thread_ts=thread_ts,
                use_plan_blocks=True,
                swap_reactions=False,
                status_callback=status_callback if batch_mode else None,
                batch_stage="Couldn't read this file" if batch_mode else None,
                batch_detail=rejection_reason,
                file_id=file_id,
                extra_return={"reason": rejection_reason},
            )

        input_page_count = estimate_upload_pages(data, source_filename)
        profile_delta["input_page_count"] = input_page_count
        firm_id = resolve_firm_id_from_state(profile_delta)
        if firm_id:
            credit_decision = credit_gate_for_bytes(
                firm_id=firm_id,
                data=data,
                filename=source_filename,
            )
            if not credit_decision.get("allowed", True):
                user_msg = credit_block_message(credit_decision)
                return _fail_doc(
                    slack_client,
                    channel_id,
                    source_filename=source_filename,
                    status_headline="❌ Out of credits",
                    user_message=user_msg,
                    return_status="blocked",
                    stage_state=_stage_state,
                    stage_error="Out of credits",
                    status_ts=status_ts,
                    upload_msg_ts=upload_msg_ts,
                    thread_ts=thread_ts,
                    status_callback=status_callback if batch_mode else None,
                    batch_stage="Out of credits" if batch_mode else None,
                    batch_detail=user_msg,
                    file_id=file_id,
                    extra_return={"reason": credit_decision.get("reason")},
                )
        else:
            # LIVE upload with no resolvable firm_id: this used to be a silent
            # skip (unbilled documents passing through unnoticed). Flag it loudly
            # and, when LEDGR_CREDIT_REQUIRE_FIRM is set, refuse the upload.
            flag_unresolved_firm_billing_anomaly(
                channel_id=channel_id,
                file_id=file_id,
                source_filename=source_filename,
            )
            if require_firm_for_billing():
                user_msg = (
                    "I couldn't determine which workspace to bill for this "
                    "document, so I didn't process it. Please contact support."
                )
                return _fail_doc(
                    slack_client,
                    channel_id,
                    source_filename=source_filename,
                    status_headline="❌ Billing not configured",
                    user_message=user_msg,
                    return_status="blocked",
                    stage_state=_stage_state,
                    stage_error="No billing firm",
                    status_ts=status_ts,
                    upload_msg_ts=upload_msg_ts,
                    thread_ts=thread_ts,
                    status_callback=status_callback if batch_mode else None,
                    batch_stage="Billing not configured" if batch_mode else None,
                    batch_detail=user_msg,
                    file_id=file_id,
                    extra_return={"reason": "no_firm"},
                )

        return await process_file_via_ledgr_agent(
            runner=runner,
            ledger_store=ledger_store,
            slack_client=slack_client,
            channel_id=channel_id,
            file_id=file_id,
            session_id=session_id,
            app_name=app_name,
            data=data,
            source_filename=source_filename,
            profile_delta=profile_delta,
            thread_ts=thread_ts,
            status_ts=status_ts,
            upload_msg_ts=upload_msg_ts,
            input_page_count=input_page_count,
            batch_mode=batch_mode,
            status_callback=status_callback,
            stage_state=_stage_state,
            client_store=client_store,
            defer_slack_delivery=defer_slack_delivery or defer_ledger_persist,
        )

def download_pdf_bytes(slack_client: Any, file_id: str) -> bytes:
    """Download a Slack file's bytes using the parked SSRF-hardened downloader.

    ``app.slack_app.slack_download_file`` streams to a temp dir (path-traversal +
    SSRF + size hardened); we read the bytes back and clean up immediately so no
    client PDF lingers on disk.
    """
    task_dir = tempfile.mkdtemp(prefix="ledgr_runner_")
    try:
        path = slack_download_file(slack_client, file_id, task_dir)
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)


_SEM = asyncio.Semaphore(_max_concurrency())
