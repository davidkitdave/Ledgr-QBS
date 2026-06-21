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
import urllib.parse
from typing import Any, Callable, Optional

# FastAPI Request/Response imported at module level so FastAPI can resolve
# the string annotations produced by `from __future__ import annotations`.
from fastapi import Request, Response

from accounting_agents.assistant import (
    LEDGER_DATA_KEY,
    PENDING_LEARN_KEY,
    PENDING_REEXTRACT_KEY,
    PENDING_WRITE_KEY,
    PROCESSING_LOG_KEY,
    THREAD_FOCUS_KEY,
)
from accounting_agents.config import _env_prefix

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
from accounting_agents.nodes import ApproveDecision, ReviewClarifyDecision
from app.blocks import (
    PIPELINE_STAGES,
    _STAGE_TITLES,
    approval_card_blocks,
    approval_outcome_blocks,
    dedup_callout_card,
    invoice_edit_modal,
    ledger_preview_data_table,
    summary_table_blocks,
    delivery_card_blocks,
    compose_batch_delivery_summary,
    job_progress_text,
    software_label,
    proactive_redo_blocks,
    proactive_redo_modal,
    processing_plan_blocks,
    review_card_blocks,
    review_hint_modal,
    review_outcome_blocks,
    confident_note_block,
)
from app.slack_app import _SeenEvents
from invoice_processing.export.client_context import FirestoreClientStore
from invoice_processing.export.exporters import (
    collect_account_flagged_summary,
    decorate_preview_account_flags,
    format_account_flagged_note,
    format_extraction_doc_count_note,
    get_exporter,
)
from invoice_processing.extract.partial_failure import format_partial_failure_note

def _strip_slack_mentions(text: str) -> str:
    import re
    return re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()

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

    Also injects the per-client familiarity map (Lever 4 / ADR-0017 §6) under
    ``nodes.FAMILIARITY_KEY`` so ``detect_struggle`` can read it without
    touching the store directly.
    """
    ctx = client_store.get_by_channel(channel_id)
    if ctx is None:
        return {}
    delta = ctx.to_state()
    # Inject familiarity map if the store supports it (InMemoryClientStore in
    # tests, FirestoreClientStore in production — both expose get_familiarity_map).
    try:
        fam_map = client_store.get_familiarity_map(ctx.client_id or "")
        delta[nodes.FAMILIARITY_KEY] = fam_map
    except Exception:  # noqa: BLE001 — familiarity load must never abort a run
        delta.setdefault(nodes.FAMILIARITY_KEY, {})
    return delta

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


#: Outcome of :func:`_offer_coa_confirmation`.
_OFFER_OUTCOME_CONFIRM_CARD = "confirm_card"
_OFFER_OUTCOME_DISAMBIG_CARD = "disambiguation_card"
_OFFER_OUTCOME_AS_DOCUMENT = "as_document"
_OFFER_OUTCOME_NO_FILE = "no_file"


async def _offer_coa_confirmation(
    *,
    sync_client,
    channel_id: str,
    file_id: str,
    file_payload: dict,
    channel_state: str,
) -> str:
    """Download a dropped spreadsheet, classify it, and post the right card.

    Returns a short outcome string the caller can use for logging / dedup
    bookkeeping. The function is best-effort: any IO or parse failure posts a
    fallback "process as document" message so the upload is never silently
    dropped.
    """
    import shutil as _shutil
    import tempfile as _tempfile

    from app.coa_detect import SpreadsheetKind, classify_spreadsheet
    from app.slack_app import slack_download_file as _dl
    from app.blocks import (
        coa_confirm_blocks,
        coa_unknown_disambiguation_blocks,
    )
    from app.coa_ingest import preview_coa_from_file

    filename = (
        (file_payload or {}).get("name")
        or (file_payload or {}).get("title")
        or "spreadsheet"
    )
    task_dir = _tempfile.mkdtemp(prefix="ledgr_coa_offer_")
    try:
        try:
            local_path = await asyncio.to_thread(_dl, sync_client, file_id, task_dir)
        except Exception:  # noqa: BLE001 - never let download fail silently
            logger.exception("coa confirm: download failed for file %s", file_id)
            sync_client.chat_postMessage(
                channel=channel_id,
                text=(
                    f":grey_question: I couldn't download `{filename}` to inspect it. "
                    "Re-upload and I'll ask whether to use it as a COA."
                ),
            )
            return _OFFER_OUTCOME_NO_FILE

        kind = classify_spreadsheet(local_path)
        logger.info(
            "coa confirm: classified file=%s as %s channel_state=%s",
            file_id, kind.value, channel_state,
        )

        if kind is SpreadsheetKind.LEDGER_CANDIDATE:
            # Spreadsheets that look like ledger exports fall through to the
            # document pipeline; no COA card needed.
            return _OFFER_OUTCOME_AS_DOCUMENT

        preview = preview_coa_from_file(local_path)
        if not preview:
            sync_client.chat_postMessage(
                channel=channel_id,
                text=(
                    f":grey_question: I couldn't read `{filename}`. "
                    "Re-upload and I'll try again."
                ),
            )
            return _OFFER_OUTCOME_NO_FILE

        if kind is SpreadsheetKind.UNKNOWN:
            sync_client.chat_postMessage(
                channel=channel_id,
                blocks=coa_unknown_disambiguation_blocks(
                    file_id=file_id, filename=filename,
                ),
            )
            return _OFFER_OUTCOME_DISAMBIG_CARD

        sync_client.chat_postMessage(
            channel=channel_id,
            blocks=coa_confirm_blocks(
                preview=preview,
                file_id=file_id,
                channel_state=channel_state,
                filename=filename,
            ),
        )
        return _OFFER_OUTCOME_CONFIRM_CARD
    finally:
        _shutil.rmtree(task_dir, ignore_errors=True)


def _channel_state_label(resolved) -> str:
    """Return ``"active"`` / ``"pending_coa"`` for the confirm card copy."""
    if resolved is None:
        return "pending_coa"
    return getattr(resolved, "status", None) or "pending_coa"


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


#: The long-running function-call name ADK yields when a tool requests
#: confirmation (``functions.REQUEST_CONFIRMATION_FUNCTION_CALL_NAME``). Pinned
#: here so the chat-lane confirm bridge does not import experimental internals.
REQUEST_CONFIRMATION_FUNCTION_CALL_NAME = "adk_request_confirmation"

#: Affirmative leading tokens / phrases for the chat-lane confirm bridge.
_AFFIRMATIVE_TOKENS: frozenset[str] = frozenset(
    {"yes", "y", "yeah", "yep", "yup", "confirm", "confirmed", "ok", "okay",
     "sure", "approve", "approved", "proceed"}
)
_AFFIRMATIVE_PHRASES: tuple[str, ...] = (
    "go ahead", "do it", "please do", "please proceed", "sounds good",
)
#: Negative tokens / phrases — checked FIRST (negative precedence).
_NEGATIVE_TOKENS: frozenset[str] = frozenset(
    {"no", "n", "nope", "cancel", "cancelled", "stop", "dont", "nevermind"}
)
_NEGATIVE_PHRASES: tuple[str, ...] = (
    "don't", "do not", "never mind", "leave it", "no thanks",
    "cancel that", "not yet", "hold on", "wait",
)


def classify_confirmation_reply(text: str) -> Optional[bool]:
    """Classify a free-text reply as confirm (True) / deny (False) / ambiguous (None).

    Matching rules (evaluated in order):
    1. NEGATIVE PRECEDENCE — if any negative token or phrase appears anywhere
       in the normalised text, return False.  Catches "no thanks", "no, not yet",
       "yes but actually no", etc.
    2. AFFIRMATIVE — leading token matches an affirmative word, or the text
       contains an affirmative phrase.
    3. Ambiguous (None) — neither matched.

    The fail-safe is ambiguous-as-None: an unrecognised reply passes through
    to the model rather than accidentally committing or refusing a write.
    """
    t = (text or "").strip().lower()
    # Strip trailing punctuation so "yes!" / "yes." match.
    t_stripped = t.rstrip(".!?,")
    if not t_stripped:
        return None

    # --- 1. Negative precedence ---
    for phrase in _NEGATIVE_PHRASES:
        if phrase in t:
            return False
    first_token = t_stripped.split()[0] if t_stripped.split() else ""
    if first_token in _NEGATIVE_TOKENS:
        return False
    # Full-string negative (e.g. bare "no", "cancel")
    if t_stripped in _NEGATIVE_TOKENS:
        return False

    # --- 2. Affirmative ---
    if first_token in _AFFIRMATIVE_TOKENS:
        return True
    if t_stripped in _AFFIRMATIVE_TOKENS:
        return True
    for phrase in _AFFIRMATIVE_PHRASES:
        if t.startswith(phrase) or phrase in t:
            return True

    return None


#: How many events back we look for an unanswered adk_request_confirmation.
#: A pending confirmation older than this window is treated as stale and ignored
#: — a "yes" typed days later to an unrelated question cannot commit a write
#: that the user has long since forgotten about (MEDIUM-3).
_CONFIRM_STALENESS_WINDOW: int = 6


def find_pending_confirmation(
    session: Any, *, staleness_window: int = _CONFIRM_STALENESS_WINDOW
) -> Optional[tuple[str, Any]]:
    """Return ``(fc_id, ToolConfirmation)`` for the MOST RECENT unanswered confirm.

    Scans the session's events newest-first for an ``adk_request_confirmation``
    long-running call whose ``id`` has not yet been answered by a later
    ``FunctionResponse`` of the same name+id.  Only events within the last
    ``staleness_window`` positions are checked — a confirmation older than that
    is treated as stale and ignored so an accidental "yes" in a later unrelated
    turn cannot trigger a write the user has forgotten about (MEDIUM-3).

    Returns ``None`` when there is no actionable pending confirmation.  The
    original requested payload is read from
    ``event.actions.requested_tool_confirmations`` so the synthesized response
    can echo it back faithfully.
    """
    events = list(getattr(session, "events", None) or [])
    # Only look within the staleness window (newest events first).
    window = events[-staleness_window:] if staleness_window > 0 else events
    answered: set[str] = set()
    for ev in reversed(window):
        # Collect already-answered confirmation ids (function responses).
        getter = getattr(ev, "get_function_responses", None)
        for fr in (getter() if callable(getter) else []) or []:
            if getattr(fr, "name", None) == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME:
                fid = getattr(fr, "id", None)
                if fid:
                    answered.add(fid)

        getcalls = getattr(ev, "get_function_calls", None)
        for fc in (getcalls() if callable(getcalls) else []) or []:
            if getattr(fc, "name", None) != REQUEST_CONFIRMATION_FUNCTION_CALL_NAME:
                continue
            fc_id = getattr(fc, "id", None)
            if not fc_id or fc_id in answered:
                continue
            # Try two sources for the ToolConfirmation object:
            # 1. event.actions.requested_tool_confirmations[fc_id] — set by our
            #    smoke-test / manual event construction (also what the ADK
            #    function_response event carries before the synthetic event is built).
            # 2. fc.args["toolConfirmation"] — the real ADK flow stores the
            #    ToolConfirmation dict here on the synthetic adk_request_confirmation
            #    FunctionCall emitted by generate_request_confirmation_event.
            actions = getattr(ev, "actions", None)
            requested = getattr(actions, "requested_tool_confirmations", None) or {}
            confirmation = requested.get(fc_id)
            if confirmation is None:
                args = getattr(fc, "args", None) or {}
                tc_dict = args.get("toolConfirmation")
                if tc_dict:
                    try:
                        from google.adk.tools.tool_confirmation import ToolConfirmation
                        confirmation = ToolConfirmation.model_validate(tc_dict)
                    except Exception:  # noqa: BLE001
                        pass
            return fc_id, confirmation
    return None


def _synthesize_confirmation_message(fc_id: str, confirmation: Any, *, confirmed: bool):
    """Build the ``Content`` that answers a pending ``adk_request_confirmation``.

    ADK expects a ``FunctionResponse`` (name+matching id) whose ``response`` is a
    ``ToolConfirmation`` dumped with ``by_alias=True``, echoing the originally
    requested payload so the re-executed tool sees its own write spec.
    """
    from google.adk.tools.tool_confirmation import ToolConfirmation

    payload = getattr(confirmation, "payload", None)
    hint = getattr(confirmation, "hint", None)
    answer = ToolConfirmation(hint=hint, confirmed=confirmed, payload=payload)
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=fc_id,
                    name=REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
                    response=answer.model_dump(by_alias=True),
                )
            )
        ],
    )


#: Maps a graph node name to the friendly status label shown while it runs. The
#: ADK workflow tags each event's ``node_info.path`` with the producing node
#: (e.g. ``"document_workflow@1/classify_node@1"``); :func:`event_node_name`
#: pulls the trailing node name and we look it up here to drive the live status.
_STAGE_LABELS: dict[str, str] = {
    "classify_node": "🔍 Taking a look at this document…",
    "extract_invoice_document_node": "📄 Understanding this document…",
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


#: Maps graph node name → canonical pipeline stage key used by _StageState.
_STAGE_KEY_MAP: dict[str, str] = {
    "classify_node": "understand",
    "extract_invoice_document_node": "understand",
    "extract_bank_node": "understand",
    "categorize_node": "policy",
    "tax_node": "policy",
    "approval_gate": "policy",
    "consolidate_node": "commit",
    "deliver_node": "commit",
}

#: Nodes whose plan-block output belongs to the understand stage (not policy).
_UNDERSTAND_OUTPUT_NODES: frozenset[str] = frozenset({
    "classify_node",
    "extract_invoice_document_node",
    "extract_bank_node",
})


def _output_stage_for_node(node_name: str) -> Optional[str]:
    """Map a graph node to the plan stage that should receive its output line."""
    if node_name in _UNDERSTAND_OUTPUT_NODES:
        return "understand"
    if node_name in ("categorize_node", "tax_node", "approval_gate"):
        return "policy"
    if node_name in ("consolidate_node", "deliver_node"):
        return "commit"
    return _STAGE_KEY_MAP.get(node_name)


def event_stage_key(event: Any) -> Optional[str]:
    """Return the canonical pipeline stage key for ``event``'s node, or ``None``."""
    name = event_node_name(event)
    if name is None:
        return None
    return _STAGE_KEY_MAP.get(name)


class _StageState:
    """Tracks ordered per-stage status for a single document run."""

    def __init__(self) -> None:
        self._stages: list[dict] = [
            {
                "task_id": key,
                "title": _STAGE_TITLES[key],
                "status": "pending",
                "output": None,
            }
            for key in PIPELINE_STAGES
        ]

    def _index(self, stage_key: str) -> Optional[int]:
        for i, s in enumerate(self._stages):
            if s["task_id"] == stage_key:
                return i
        return None

    def advance(self, stage_key: str, *, output: str | None = None) -> None:
        """Mark stages before stage_key complete, stage_key in_progress, rest pending.

        When output is provided, it is attached to the stage immediately before
        stage_key (the stage that just finished).
        """
        idx = self._index(stage_key)
        if idx is None:
            return
        for i, s in enumerate(self._stages):
            if i < idx:
                s["status"] = "complete"
            elif i == idx:
                s["status"] = "in_progress"
            else:
                s["status"] = "pending"
        if output is not None and idx > 0:
            self._stages[idx - 1]["output"] = output

    def mark_complete(self, *, output: str | None = None) -> None:
        """Mark all stages complete."""
        for s in self._stages:
            s["status"] = "complete"
        if output is not None and self._stages:
            self._stages[-1]["output"] = output

    def mark_failed(self, stage_key: str, error: str) -> None:
        """Mark stage_key failed; stages after it remain pending."""
        idx = self._index(stage_key)
        for i, s in enumerate(self._stages):
            if i < idx if idx is not None else False:
                s["status"] = "complete"
            elif i == idx:
                s["status"] = "failed"
                s["output"] = error

    def set_output(self, stage_key: str, output: str) -> None:
        """Attach or refresh the output line on an in-progress or complete stage."""
        idx = self._index(stage_key)
        if idx is not None:
            self._stages[idx]["output"] = output

    def snapshot(self) -> list[dict]:
        """Return a copy of the current stage list."""
        return [dict(s) for s in self._stages]


def deslugify_channel_name(name: str) -> str:
    """Turn a Slack channel slug into a human client name for modal pre-fill.

    ``"sample-channel-client-pte-ltd"`` → ``"Sample Channel Client Pte Ltd"``. Splits on
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
    replace: bool = False,
    defer_slack_delivery: bool = False,
    batch_mode: bool = False,  # noqa: ARG001 - threaded for symmetry; not used here
    defer_ledger_persist: bool = False,
    client_store=None,
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

    When ``defer_ledger_persist=True`` (set by the batch coordinator in
    ``accounting_agents/slack_runner._message`` for multi-file drops so the
    workbook is rewritten exactly ONCE at batch end), this function skips the
    ``append_rows`` call and instead returns a ``deferred_ledger`` entry on the
    result so the batch coordinator can merge it with the other stashed rows and
    write the workbook in a single pass.
    """
    if user_id is None:
        user_id = session_id
    session = await runner.session_service.get_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )
    state = dict(session.state) if session else {}
    payload = state.get(nodes.LEDGER_ROWS_KEY) or {}
    batches = payload.get("batches") or []

    # Re-extract (ADR-0010): the corrected read pauses at the HITL gate, so the
    # ``replace`` intent is seeded into run state (``reextract_replace``) and read
    # here — it survives the pause and the resume path (handle_review_action) too.
    # The explicit ``replace`` kwarg (fresh-run callers) takes precedence.
    effective_replace = bool(replace) or bool(state.get("reextract_replace"))
    # Belt-and-suspenders: clear the flag the moment it is consumed so it cannot
    # leak into a future normal drop on the same long-lived session. This covers
    # the HITL-resumed path (handle_review_action → _finalize_run_outcome →
    # persist_and_deliver) where process_file_event's unconditional state_delta
    # reset does not re-run.
    if state.get("reextract_replace"):
        await _apply_state_delta(
            runner, app_name, user_id, session_id, {"reextract_replace": False}
        )

    append_result: dict = {}
    if batches and not defer_ledger_persist:
        append_result = await asyncio.to_thread(
            ledger_store.append_rows,
            client_id=payload.get("client_id") or "unknown",
            fy=str(payload.get("fy") or "unknown"),
            slack_client=slack_client,
            channel_id=channel_id,
            batches=batches,
            software=payload.get("software") or "",
            kind=payload.get("kind") or "invoice",
            client_name=payload.get("client_name") or "",
            replace=effective_replace,
        )
        # Carry context forward so the batch tally can label the destination
        # accurately (bank statement vs ledger) without re-reading the payload.
        append_result.setdefault("kind", payload.get("kind") or "invoice")
        append_result.setdefault("software", payload.get("software") or "")
        append_result.setdefault("fy", str(payload.get("fy") or ""))
    elif batches and defer_ledger_persist:
        # Stash the ledger payload so the batch coordinator can merge with peers
        # and call ``append_rows`` once per FY workbook. The ``deferred_ledger``
        # key mirrors the existing ``deferred_delivery`` shape: the coordinator
        # reads it after the per-doc loop, calls ledger_store.append_rows with the
        # merged batches, and threads the final workbook name into
        # ``deferred_delivery["workbook_name"]`` so the aggregate delivery card
        # references the right file.
        summary = state.get(nodes.DELIVER_SUMMARY_KEY) or nodes.compose_delivery_summary(payload)
        append_result = {
            "deferred_ledger": {
                "summary": summary,
                "batches": batches,
                "payload": payload,
                "effective_replace": effective_replace,
            },
            "kind": payload.get("kind") or "invoice",
            "software": payload.get("software") or "",
            "fy": str(payload.get("fy") or ""),
            "appended": 0,  # not yet appended; batch-end call will set this
        }

    # When every batch was deduped
    # dedup callout card with [Replace recorded month] and [Keep existing] buttons
    # so the user can take action without re-uploading.
    if append_result.get("deduped", 0) > 0 and append_result.get("appended", 0) == 0:
        kind = payload.get("kind") or "invoice"
        fy_str = str(payload.get("fy") or "")
        fy_int = int(fy_str) if fy_str.isdigit() else 0
        workbook = append_result.get("filename") or ""
        if kind == "bank":
            all_rows = [r for b in batches for r in (b.get("rows") or [])]
            month = nodes._month_label(all_rows) or "this month"
            vendor = payload.get("client_name") or "bank statement"
        else:
            dk = str(batches[0].get("doc_key") or "") if batches else ""
            inv_num = dk.split(":")[-1] if ":" in dk else ""
            # Vendor: extract from the first batch row's Supplier/Customer column
            first_rows = batches[0].get("rows") or [] if batches else []
            vendor = (
                (first_rows[0].get("Supplier") or first_rows[0].get("Customer") or "")
                if first_rows else ""
            ) or inv_num or "this vendor"
            # Month: derive from the Date column of the first batch row
            all_rows = [r for b in batches for r in (b.get("rows") or [])]
            month = nodes._month_label(all_rows) or "this month"
        n_incoming = sum(len(b.get("rows") or []) for b in batches)
        existing: dict = {"rows": 0, "date_range": month, "workbook": workbook}
        incoming_info: dict = {"rows": n_incoming, "date_range": month, "file_label": workbook}
        blocks = dedup_callout_card(
            vendor=vendor,
            fy=fy_int,
            month=month,
            existing=existing,
            incoming=incoming_info,
            channel_id=channel_id,
        )
        kwargs: dict = {"channel": channel_id, "blocks": blocks,
                        "text": f"⚠️ Already recorded: {month} invoices for {vendor}"}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        slack_client.chat_postMessage(**kwargs)
        append_result["all_deduped"] = True
        return append_result

    # HITL-approve path skips deliver_node, so DELIVER_SUMMARY_KEY is absent.
    # Derive the summary from the LEDGER_ROWS_KEY payload so the HITL path emits
    # the SAME rich delivery card as the clean path — never the bare fallback.
    summary = state.get(nodes.DELIVER_SUMMARY_KEY) or nodes.compose_delivery_summary(payload)

    if defer_slack_delivery:
        if append_result.get("appended", 0) > 0 or append_result.get("deferred_ledger"):
            append_result["deferred_delivery"] = {
                "summary": summary,
                "batches": batches,
                "payload": payload,
                "workbook_name": append_result.get("filename") or "",
            }
        # Phase 1 (thread-context fix): persist processing_log BEFORE the early
        # return so multi-file batch drops stay visible to the chat lane. The
        # delivery_message_ts is the per-doc thread_ts at this point; the batch
        # coordinator patches it to the job-summary ts at batch end via
        # ``_patch_processing_log_thread`` (see _message handler).
        if batches and not append_result.get("all_deduped"):
            _record_processing_log(
                state=state,
                payload=payload,
                batches=batches,
                append_result=append_result,
                client_store=client_store or _DEFAULT_CLIENT_STORE,
                delivery_message_ts=thread_ts,
                channel_id=channel_id,
            )
        return append_result

    if append_result.get("appended", 0) > 0 and batches:
        _post_delivery_card(
            slack_client,
            channel_id,
            summary=summary,
            batches=batches,
            payload=payload,
            append_result=append_result,
            thread_ts=thread_ts,
        )
    elif summary and not append_result.get("all_deduped"):
        _post_message(slack_client, channel_id, summary, thread_ts=thread_ts)

    if batches and not append_result.get("all_deduped"):
        _record_processing_log(
            state=state,
            payload=payload,
            batches=batches,
            append_result=append_result,
            client_store=client_store or _DEFAULT_CLIENT_STORE,
            delivery_message_ts=thread_ts,
            channel_id=channel_id,
        )

    return append_result


def _record_processing_log(
    *,
    state: dict,
    payload: dict,
    batches: list[dict],
    append_result: dict,
    client_store,
    delivery_message_ts: Optional[str] = None,
    channel_id: Optional[str] = None,
    invoice_ids: Optional[list[str]] = None,
) -> None:
    """Persist extraction metadata so the chat assistant can introspect deliveries.

    ``delivery_message_ts`` and ``channel_id`` (Phase 2) are the Slack message
    timestamp the delivery card is parented under and the channel it lives in;
    they let the chat lane resolve a thread reply back to the specific batch the
    user is asking about (``raw_thread_ts`` in answer_question).
    """
    client_id = str(payload.get("client_id") or state.get("client_id") or "").strip()
    # File id may be on top-level state (fresh ADK run) OR inside the ledger payload
    # (older session snapshot); accept both so the entry always has a stable id.
    file_id = str(
        state.get("file_id")
        or payload.get("file_id")
        or ""
    ).strip()
    if not client_id or not file_id:
        return
    from datetime import datetime, timezone

    doc_type = str(state.get(nodes.DOC_TYPE_KEY) or payload.get("kind") or "invoice").strip().lower()
    extraction_path = str(state.get(nodes.EXTRACTION_PATH_KEY) or "unknown").strip().lower()
    row_count = sum(len(b.get("rows") or []) for b in batches)
    if row_count == 0:
        summary_table = (
            state.get("ledger_summary_table")
            or state.get("summary_table")
            or []
        )
        if isinstance(summary_table, list) and summary_table:
            row_count = len(summary_table)
        else:
            try:
                row_count = int(state.get("row_count") or 0)
            except (TypeError, ValueError):
                row_count = 0
    if not invoice_ids:
        invoice_ids = _invoice_ids_from_batches(batches)
    entry = {
        "file_id": file_id,
        "filename": str(state.get("source_filename") or file_id),
        "doc_type": doc_type,
        "extraction_path": extraction_path,
        "delivered_at": datetime.now(timezone.utc).isoformat(),
        "row_count": row_count,
        "fy": str(payload.get("fy") or append_result.get("fy") or ""),
        "soa_legacy_path": doc_type == "statement_of_account" or extraction_path == "legacy",
    }
    # Phase 2: thread-context linkage. Optional keys only — older entries (pre-fix)
    # simply lack them and the resolver skips.
    if delivery_message_ts:
        entry["delivery_message_ts"] = delivery_message_ts
    if channel_id:
        entry["channel_id"] = channel_id
    if invoice_ids:
        entry["invoice_ids"] = list(invoice_ids)
    try:
        client_store.append_processing_log(client_id=client_id, file_id=file_id, entry=entry)
    except Exception:  # noqa: BLE001 — log write is best-effort
        logger.exception(
            "processing_log write failed for client=%s file=%s", client_id, file_id
        )


def _extraction_doc_count_blocks(
    payload: dict,
    *,
    file_label: str | None = None,
) -> list[dict]:
    """WS-2.4 — G3 doc-count context block when extraction metadata exists."""
    if (payload.get("kind") or "invoice") != "invoice":
        return []
    doc_count = payload.get("extracted_doc_count")
    page_count = payload.get("input_page_count")
    if doc_count is None or page_count is None:
        return []
    note = format_extraction_doc_count_note(int(doc_count), int(page_count))
    if not note:
        return []
    if file_label:
        note = f"📄 *{file_label}* — {note}"
    blocks = [confident_note_block(note)]
    partial = payload.get("partial_failure_warnings") or []
    partial_note = format_partial_failure_note(partial)
    if partial_note:
        if file_label:
            partial_note = f"📄 *{file_label}* — {partial_note}"
        blocks.append(confident_note_block(partial_note))
    return blocks


def _post_delivery_card(
    slack_client: Any,
    channel_id: str,
    *,
    summary: str,
    batches: list[dict],
    payload: dict,
    append_result: dict,
    thread_ts: Optional[str] = None,
) -> None:
    """Post one delivery message: summary + ledger preview data_table(s)."""
    workbook_name = append_result.get("filename") or "Ledger.xlsx"
    fy_str = append_result.get("fy") or str(payload.get("fy") or "")
    try:
        fy_int = int(fy_str)
    except (TypeError, ValueError):
        fy_int = 0
    software = str(payload.get("software") or "qbs_ledger")
    preview_blocks: list[dict] = []
    try:
        preview_exporter = get_exporter(software)
    except Exception:  # noqa: BLE001 — preview decoration is cosmetic
        preview_exporter = None
    for batch in batches:
        batch_rows = batch.get("rows") or []
        if not batch_rows:
            continue
        sheet = str(batch.get("sheet") or "Purchase")
        row_doc_type = "sales" if sheet == "Sales" else "purchase"
        preview_rows = batch_rows
        if preview_exporter is not None:
            try:
                preview_rows = decorate_preview_account_flags(
                    batch_rows, preview_exporter, row_doc_type
                )
            except Exception:  # noqa: BLE001 — preview decoration is cosmetic
                logger.warning(
                    "account-flag preview decoration failed (non-fatal)", exc_info=True
                )
        try:
            preview_blocks.extend(
                ledger_preview_data_table(
                    rows=preview_rows,
                    workbook_name=workbook_name,
                    fy=fy_int,
                    sheet=sheet,
                    software=software,
                    channel_id=channel_id,
                )
            )
        except Exception:  # noqa: BLE001 — preview is cosmetic
            logger.warning(
                "ledger preview build failed for sheet %s (non-fatal)", sheet, exc_info=True
            )
    blocks = (
        delivery_card_blocks(summary, preview_blocks)
        if preview_blocks
        else [{"type": "section", "text": {"type": "mrkdwn", "text": summary}}]
    )
    blocks.extend(_extraction_doc_count_blocks(payload))
    # Confident-path note (ADR-0017 Lever 1): only fires on the clean no-pause
    # delivery path.  ``payload["delivered"]`` is True only when deliver_node ran
    # (the clean path); the HITL-approve path bypasses deliver_node so the key is
    # False/absent — ensuring a human-reviewed doc never shows "no pause needed".
    # NOTE: this note is single-doc-scoped only.  The batch-aggregate path
    # (_build_batch_aggregate_blocks) does not receive per-doc payloads in the same
    # shape and is intentionally excluded here; per-doc notes in batch summaries are
    # deferred to the Step 5 batch-delivery rework.
    doc_type = str(payload.get("doc_type") or "").strip().lower()
    if doc_type in ("expense_claim", "other") and payload.get("delivered"):
        try:
            free_type = payload.get("free_type") or None
            note = nodes.compose_confident_note(
                payload, doc_type=doc_type, free_type=free_type
            )
            if note:
                blocks.append(confident_note_block(note))
        except Exception:  # noqa: BLE001 — note is cosmetic
            logger.warning("confident note build failed (non-fatal)", exc_info=True)
    # Import-readiness checklist for AutoCount / SQL Account purchase/sales deliveries.
    # Skipped when doc_type is expense_claim/other (compose_confident_note already
    # embeds the readiness note inside the confident note above).
    if doc_type not in ("expense_claim", "other"):
        try:
            from invoice_processing.export.exporters import normalize_software_key as _nsk
            if _nsk(software) in ("autocount", "sql_account"):
                rnote = nodes.format_import_readiness_note(payload.get("import_readiness"))
                if rnote:
                    blocks.append(confident_note_block(rnote))
        except Exception:  # noqa: BLE001 — readiness note is cosmetic
            logger.warning("import readiness note build failed (non-fatal)", exc_info=True)
    # WS-3.4 — surface low-confidence COA picks on QBS/Xero deliveries (profile
    # ERPs embed the same warning inside format_import_readiness_note).
    try:
        from invoice_processing.export.exporters import normalize_software_key as _nsk

        if _nsk(software) not in ("autocount", "sql_account"):
            flagged_note = format_account_flagged_note(
                collect_account_flagged_summary(batches)
            )
            if flagged_note:
                blocks.append(confident_note_block(flagged_note))
    except Exception:  # noqa: BLE001 — cosmetic
        logger.warning("account-flagged delivery note failed (non-fatal)", exc_info=True)
    kwargs: dict = {"channel": channel_id, "text": summary, "blocks": blocks}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    try:
        slack_client.chat_postMessage(**kwargs)
    except Exception:  # noqa: BLE001 — cosmetic; never break delivery
        logger.warning("delivery card post failed (non-fatal)", exc_info=True)


def _build_batch_aggregate_blocks(
    deferred_items: list[dict],
    channel_id: str,
) -> tuple[str, list[dict]]:
    """Build the aggregate delivery summary + data-table blocks for a batch.

    Returns ``(summary_text, blocks)``. Caller is responsible for posting
    (single post) or appending (chat.update merging with the job-summary
    message so the whole batch lives in ONE top-level message).
    """
    if not deferred_items:
        return "", []

    sheet_groups: dict[tuple, dict] = {}
    summary_groups: dict[str, dict] = {}
    client_name = ""

    for item in deferred_items:
        payload = item.get("payload") or {}
        batches = item.get("batches") or []
        workbook_name = item.get("workbook_name") or "Ledger.xlsx"
        fy = str(payload.get("fy") or "")
        software = str(payload.get("software") or "")
        kind = str(payload.get("kind") or "invoice")
        client_name = client_name or str(payload.get("client_name") or "")

        sg = summary_groups.setdefault(
            fy,
            {"fy": fy, "software": software, "kind": kind, "n_rows": 0, "n_docs": 0,
             "client_name": client_name},
        )
        sg["n_rows"] += sum(len(b.get("rows") or []) for b in batches)
        # n_docs counts documents, not per-sheet batches — a single doc that
        # splits into Purchase+Sales sheets is one document, not two.
        sg["n_docs"] += 1

        for batch in batches:
            batch_rows = batch.get("rows") or []
            if not batch_rows:
                continue
            sheet = str(batch.get("sheet") or "Purchase")
            key = (fy, sheet, workbook_name, software, kind)
            grp = sheet_groups.setdefault(
                key,
                {"rows": [], "fy": fy, "sheet": sheet, "workbook_name": workbook_name,
                 "software": software},
            )
            grp["rows"].extend(batch_rows)

    summary = compose_batch_delivery_summary(
        groups=list(summary_groups.values()),
        client_name=client_name,
    )
    preview_blocks: list[dict] = []
    for grp in sheet_groups.values():
        try:
            fy_int = int(grp["fy"])
        except (TypeError, ValueError):
            fy_int = 0
        sheet = str(grp.get("sheet") or "Purchase")
        row_doc_type = "sales" if sheet == "Sales" else "purchase"
        preview_rows = grp["rows"]
        try:
            batch_exporter = get_exporter(str(grp.get("software") or ""))
            preview_rows = decorate_preview_account_flags(
                grp["rows"], batch_exporter, row_doc_type
            )
        except Exception:  # noqa: BLE001 — preview decoration is cosmetic
            logger.warning(
                "batch account-flag preview decoration failed (non-fatal)", exc_info=True
            )
        preview_blocks.extend(
            ledger_preview_data_table(
                rows=preview_rows,
                workbook_name=grp["workbook_name"],
                fy=fy_int,
                sheet=grp["sheet"],
                software=grp["software"],
                channel_id=channel_id,
            )
        )

    blocks = (
        delivery_card_blocks(summary, preview_blocks)
        if preview_blocks
        else [{"type": "section", "text": {"type": "mrkdwn", "text": summary}}]
    )

    # AR2 / WS-1.3 — render the per-doc confident note + import-readiness
    # checklist on the batch-aggregate path too. The single-file path
    # (_post_delivery_card) already does this; the multi-file drop is the
    # COMMON path for the user, and previously showed neither a reconcile
    # total nor a readiness note (the batch cards were "blind"). Iterate
    # deferred_items in the order the user dropped them and append a
    # confident_note_block per item that has one. Errors are non-fatal —
    # the cosmetic notes never break delivery.
    for item in deferred_items:
        item_payload = item.get("payload") or {}
        item_doc_type = str(item_payload.get("doc_type") or "").strip().lower()
        item_software = str(item_payload.get("software") or "")
        if not item_payload:
            continue
        try:
            _label = (
                item_payload.get("workbook_label")
                or item_payload.get("source_file")
                or None
            )
            blocks.extend(
                _extraction_doc_count_blocks(
                    item_payload,
                    file_label=_label if len(deferred_items) > 1 else None,
                )
            )
            if item_doc_type in ("expense_claim", "other") and item_payload.get("delivered"):
                free_type = item_payload.get("free_type") or None
                note = nodes.compose_confident_note(
                    item_payload, doc_type=item_doc_type, free_type=free_type,
                )
                if note:
                    blocks.append(confident_note_block(note))
            elif item_doc_type not in ("expense_claim", "other"):
                from invoice_processing.export.exporters import (
                    compute_doc_flag_breakdown,
                    format_flag_breakdown_note,
                    get_exporter as _get_exporter,
                    normalize_software_key as _nsk,
                )
                if _nsk(item_software) in ("autocount", "sql_account"):
                    rnote = nodes.format_import_readiness_note(
                        item_payload.get("import_readiness"),
                    )
                    if rnote:
                        blocks.append(confident_note_block(rnote))
                    # WS-1.4 — per-doc ✓/✗ reconcile status + flag-reason
                    # breakdown. Counts were already computed by
                    # ``collect_export_unmapped_summary`` for the readiness
                    # path; we recompute here per-doc so the per-reason
                    # counts (blank account / missing tax / missing
                    # creditor) surface on the delivery card instead of
                    # being discarded in the aggregate unmapped count.
                    try:
                        _exp = _get_exporter(item_software)
                        _batches = item.get("batches") or item_payload.get("batches") or []
                        _breakdown = compute_doc_flag_breakdown(_batches, _exp)
                        _flag_note = format_flag_breakdown_note(_breakdown)
                        if _flag_note:
                            # Prepend the doc label so the user can see
                            # which file each status applies to.
                            _label = (
                                item_payload.get("workbook_label")
                                or item_payload.get("source_file")
                                or item_payload.get("client_name")
                                or "document"
                            )
                            blocks.append(confident_note_block(
                                f"📄 *{_label}* — {_flag_note}"
                            ))
                    except Exception:  # noqa: BLE001 — flag breakdown is cosmetic
                        logger.warning(
                            "batch per-doc flag breakdown failed (non-fatal)",
                            exc_info=True,
                        )
        except Exception:  # noqa: BLE001 — notes are cosmetic
            logger.warning(
                "batch per-item note build failed (non-fatal)", exc_info=True,
            )

    return summary, blocks


def _bank_batch_dedup_callout(
    deferred_items: list[dict],
    flush_results: list[dict],
    channel_id: str,
) -> tuple[list[dict], str]:
    """Build Replace/Keep dedup card when a bank batch fully deduped at flush.

    Returns ``(blocks, stash_key)`` where ``stash_key`` keys pending replace
    payloads for the ``ledgr_dedup_replace`` action handler.
    """
    if not deferred_items or not flush_results:
        return [], ""
    deduped = sum(int(r.get("deduped") or 0) for r in flush_results)
    appended = sum(int(r.get("appended") or 0) for r in flush_results)
    if deduped == 0 or appended > 0:
        return [], ""

    payload = (deferred_items[0].get("payload") or {})
    if (payload.get("kind") or "invoice") != "bank":
        return [], ""

    batches: list[dict] = []
    for item in deferred_items:
        batches.extend(item.get("batches") or [])
    if not batches:
        return [], ""

    all_rows = [r for b in batches for r in (b.get("rows") or [])]
    txn_rows = [
        r for r in all_rows
        if (r.get("Description") or "") not in ("BALANCE B/F", "TOTALS")
    ]
    month = nodes._month_label(txn_rows) or "this month"
    fy_str = str(payload.get("fy") or "0")
    try:
        fy_int = int(fy_str)
    except (TypeError, ValueError):
        fy_int = 0
    vendor = payload.get("client_name") or "bank statement"
    workbook = (flush_results[0].get("filename") or "") if flush_results else ""
    n_incoming = len(txn_rows)
    client_id = str(payload.get("client_id") or "")

    stash_key = f"{client_id}|{channel_id}|{fy_str}|{month}"
    blocks = dedup_callout_card(
        vendor=vendor,
        fy=fy_int,
        month=month,
        existing={"rows": n_incoming, "date_range": month, "workbook": workbook},
        incoming={"rows": n_incoming, "date_range": month, "file_label": workbook},
        op_id=stash_key,
        channel_id=channel_id,
    )
    return blocks, stash_key


def _stash_bank_dedup_replace(
    ledger_store: Any,
    deferred_items: list[dict],
    *,
    stash_key: str,
) -> None:
    """Persist incoming bank batches so Replace can re-merge without re-upload."""
    if not hasattr(ledger_store, "stash_bank_dedup_replace"):
        return
    batches: list[dict] = []
    payload: dict = {}
    for item in deferred_items:
        payload = item.get("payload") or payload
        batches.extend(item.get("batches") or [])
    if not batches:
        return
    ledger_store.stash_bank_dedup_replace(
        stash_key=stash_key,
        client_id=str(payload.get("client_id") or ""),
        fy=str(payload.get("fy") or ""),
        kind=str(payload.get("kind") or "bank"),
        software=str(payload.get("software") or ""),
        client_name=str(payload.get("client_name") or ""),
        batches=batches,
    )


def _post_batch_aggregate_delivery(
    slack_client: Any,
    channel_id: str,
    deferred_items: list[dict],
) -> None:
    """One aggregate delivery card after a multi-file batch completes."""
    summary, blocks = _build_batch_aggregate_blocks(deferred_items, channel_id)
    if not summary:
        return
    try:
        slack_client.chat_postMessage(
            channel=channel_id,
            text=summary,
            blocks=blocks,
        )
    except Exception:  # noqa: BLE001
        logger.warning("batch aggregate delivery post failed (non-fatal)", exc_info=True)


async def _flush_deferred_ledger_writes(
    *,
    ledger_store: SlackLedgerStore,
    slack_client: Any,
    channel_id: str,
    batch_deferred: list[dict],
) -> list[dict]:
    """Merge stashed ledger payloads across the batch and write the workbook ONCE.

    Each per-doc run in batch mode added a ``deferred_ledger`` to its result
    (carrying its own ``batches`` list, ``payload`` and ``effective_replace``
    flag). We group by ``(client_id, fy, software, kind)`` and call
    :meth:`SlackLedgerStore.append_rows` once per group. The merged
    ``workbook_name`` is then back-patched onto each ``deferred_delivery`` entry
    so the aggregate delivery card references the right file.

    Returns one :meth:`SlackLedgerStore.append_rows` result dict per FY group
    (empty when nothing was stashed). Callers use ``appended`` / ``deduped`` to
    reconcile the Job summary and decide whether to show delivery preview tables.

    Errors here are non-fatal: the delivery message still posts; the workbook
    may simply miss the late write until a later file drop re-runs the same FY.
    """
    # Group deferred_ledger entries by (client_id, fy, software, kind).
    # ``client_id`` and ``software`` may be missing on some payloads; fall back
    # to the "hint" values collected on the result for that doc.
    groups: dict[tuple[str, str, str, str], dict] = {}
    for item in batch_deferred:
        payload = item.get("payload") or {}
        client_id = payload.get("client_id") or "unknown"
        fy = str(payload.get("fy") or "unknown")
        software = payload.get("software") or ""
        kind = payload.get("kind") or "invoice"
        key = (client_id, fy, software, kind)
        grp = groups.setdefault(
            key,
            {
                "client_id": client_id,
                "fy": fy,
                "software": software,
                "kind": kind,
                "client_name": payload.get("client_name") or "",
                "batches": [],
                "effective_replace": False,
            },
        )
        # Concatenate this doc's batches into the group. The ledger store
        # dedupes on doc_key, so even if we re-stash the same doc_id twice
        # (rare) it won't double-write.
        grp["batches"].extend(item.get("batches") or [])
        if item.get("effective_replace"):
            grp["effective_replace"] = True

    if not groups:
        return []

    flush_results: list[dict] = []
    for grp in groups.values():
        if not grp["batches"]:
            continue
        try:
            append_result = await asyncio.to_thread(
                ledger_store.append_rows,
                client_id=grp["client_id"],
                fy=grp["fy"],
                slack_client=slack_client,
                channel_id=channel_id,
                batches=grp["batches"],
                software=grp["software"],
                kind=grp["kind"],
                client_name=grp["client_name"],
                replace=grp["effective_replace"],
            )
        except Exception:  # noqa: BLE001 — non-fatal; delivery card still posts
            logger.exception(
                "batch-end workbook append failed for client=%s fy=%s kind=%s",
                grp["client_id"], grp["fy"], grp["kind"],
            )
            continue
        flush_results.append(append_result or {})
        workbook_name = (append_result or {}).get("filename") or ""
        if not workbook_name:
            continue
        # Back-patch the resolved workbook name onto every deferred delivery
        # entry that belongs to this (client, fy) group.
        for item in batch_deferred:
            payload = item.get("payload") or {}
            if (
                (payload.get("client_id") or "unknown") == grp["client_id"]
                and str(payload.get("fy") or "unknown") == grp["fy"]
                and (payload.get("kind") or "invoice") == grp["kind"]
            ):
                item["workbook_name"] = workbook_name
    return flush_results


def _terminal_status_line(append_result: dict, _payload: Optional[dict] = None) -> str:
    """One-line status after delivery — no plan accordion."""
    fy = append_result.get("fy") or (_payload or {}).get("fy") or "?"
    sw = software_label(str(append_result.get("software") or (_payload or {}).get("software") or ""))
    kind = append_result.get("kind") or (_payload or {}).get("kind") or "invoice"
    noun = "Bank Statement" if kind == "bank" else "Ledger"
    sw_bit = f" ({sw})" if sw else ""
    return f"✅ Added to {noun} FY{fy}{sw_bit}"


def _simple_status_blocks(text: str) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _build_preview_rows(batches: list[dict]) -> list[dict]:
    """Convert exporter-column batch rows to the canonical preview shape.

    Accepts both QBS and Xero exporter column names.  Returns a flat list of
    dicts with keys: ``date``, ``description``, ``account_code``, ``tax_code``,
    ``net``, ``total``.
    """
    result: list[dict] = []
    for batch in batches:
        for row in batch.get("rows") or []:
            if not isinstance(row, dict):
                continue
            date_val = (
                row.get("date")
                or row.get("Invoice Date")
                or row.get("*InvoiceDate")
                or row.get("Date")
                or ""
            )
            desc_val = (
                row.get("description")
                or row.get("Description")
                or row.get("*Description")
                or ""
            )
            acct_val = (
                row.get("account_code")
                or row.get("Account Code / COA")
                or row.get("*AccountCode")
                or ""
            )
            tax_val = (
                row.get("tax_code")
                or row.get("Tax Code")
                or row.get("*TaxType")
                or ""
            )
            # Net: Sub Total (QBS purchase), Amount (QBS sales/Xero purchase)
            net_val = (
                row.get("net")
                or row.get("Sub Total")
                or row.get("Source Amount")
                or row.get("Amount")
                or row.get("*UnitAmount")
                or 0
            )
            # Total: Total Amount (QBS purchase), Total (QBS sales/Xero)
            total_val = (
                row.get("total")
                or row.get("Total Amount")
                or row.get("Total")
                or 0
            )
            result.append({
                "date": str(date_val) if date_val else "",
                "description": str(desc_val) if desc_val else "",
                "account_code": str(acct_val) if acct_val else "",
                "tax_code": str(tax_val) if tax_val else "",
                "net": net_val,
                "total": total_val,
            })
    return result


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


def _resolve_file_channel(
    slack_client: Any, file_id: str
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(channel_id, upload_ts)`` for ``file_id`` from ``files_info``.

    ``file.shares.{public,private}`` is a dict keyed by CHANNEL id, each value a
    list of share records carrying the upload ``ts``.  The proactive re-extract
    view-submission body has no reliable channel context (Slack does not echo the
    source channel on a ``view_submission``), so we recover it from the file's own
    share record — the same ``files_info`` shape :func:`_resolve_file_message_ts`
    reads.  Returns ``(None, None)`` on any error or missing data.
    """
    try:
        resp = slack_client.files_info(file=file_id)
        data = resp.data if hasattr(resp, "data") else resp
        if not isinstance(data, dict):
            return (None, None)
        file_obj = data.get("file") or {}
        shares = file_obj.get("shares") or {}
        for bucket in ("private", "public"):
            channels = shares.get(bucket) or {}
            for channel_id, records in channels.items():
                if records:
                    ts = records[0].get("ts")
                    return (channel_id, ts)
    except Exception:  # noqa: BLE001 - best-effort channel recovery
        logger.debug("files_info(channel) failed for file %s", file_id)
    return (None, None)


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
    except Exception as exc:  # noqa: BLE001 - cosmetic
        err = str(exc).lower()
        if "missing_scope" in err or "not_allowed_token" in err:
            logger.warning(
                "reactions_add(%s) blocked (missing reactions:write?) — "
                "reinstall the Slack app from slack/manifest*.json",
                name,
            )
        else:
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
# Chat-lane agentic UX (Phase 4): eyes ack + assistant thinking status
# --------------------------------------------------------------------------- #

_CHAT_TOOL_LOADING: dict[str, str] = {
    "lookup_row": "Looking up that invoice in the ledger…",
    "explain_categorization": "Checking why this account code was chosen…",
    "explain_tax_treatment": "Checking the tax treatment…",
    "diagnose_assistant_context": "Checking what's loaded for this client…",
    "get_document_processing_detail": "Reviewing how that document was extracted…",
    "list_recent_documents": "Listing recent documents…",
    "list_processing_history": "Checking processing history…",
    "summarize_recent_activity": "Summarizing recent activity…",
}

_CHAT_LOADING_MESSAGES: tuple[str, ...] = (
    "Working on your question…",
    "Checking your ledger…",
    "Reviewing the loaded data…",
)


def _chat_ux_enabled() -> bool:
    """Feature flag for chat-lane Slack UX (default on). Set ``LEDGR_CHAT_UX=0`` to disable."""
    return os.getenv("LEDGR_CHAT_UX", "1").strip().lower() not in ("0", "false", "no")


def _chat_stream_enabled() -> bool:
    """Optional Phase 5 streaming (default off). Requires slack-sdk ≥ 3.40."""
    return os.getenv("LEDGR_CHAT_STREAM", "0").strip().lower() in ("1", "true", "yes")


def _ack_chat_turn(slack_client: Any, channel_id: str, user_message_ts: Optional[str]) -> None:
    """Instant 👀 on the user's message so they know the agent saw it."""
    if _chat_ux_enabled():
        _add_reaction(slack_client, channel_id, user_message_ts, "eyes")


def _set_chat_thinking(
    slack_client: Any,
    channel_id: str,
    thread_ts: Optional[str],
    *,
    stage: Optional[str] = None,
) -> None:
    """Show Slack's native 'is thinking…' shimmer in the thread."""
    if not _chat_ux_enabled() or not thread_ts:
        return
    loading = list(_CHAT_LOADING_MESSAGES)
    if stage:
        loading.insert(0, stage)
    try:
        slack_client.assistant_threads_setStatus(
            channel_id=channel_id,
            thread_ts=thread_ts,
            status="is thinking...",
            loading_messages=loading[:10],
        )
    except Exception:  # noqa: BLE001 — cosmetic
        logger.debug(
            "assistant_threads_setStatus failed channel=%s thread=%s",
            channel_id, thread_ts, exc_info=True,
        )


def _clear_chat_thinking(slack_client: Any, channel_id: str, thread_ts: Optional[str]) -> None:
    """Clear the thinking shimmer (Slack also auto-clears on reply)."""
    if not _chat_ux_enabled() or not thread_ts:
        return
    try:
        slack_client.assistant_threads_setStatus(
            channel_id=channel_id,
            thread_ts=thread_ts,
            status="",
        )
    except Exception:  # noqa: BLE001 — cosmetic
        logger.debug(
            "assistant_threads_setStatus(clear) failed channel=%s thread=%s",
            channel_id, thread_ts, exc_info=True,
        )


def _finish_chat_ack(
    slack_client: Any, channel_id: str, user_message_ts: Optional[str]
) -> None:
    """Swap 👀 for ✅ on the user's message after a successful reply."""
    if not _chat_ux_enabled() or not user_message_ts:
        return
    _remove_reaction(slack_client, channel_id, user_message_ts, "eyes")
    _add_reaction(slack_client, channel_id, user_message_ts, "white_check_mark")


def _function_call_names(event: Any) -> list[str]:
    """Return tool names from an ADK event's function-call parts."""
    getter = getattr(event, "get_function_calls", None)
    calls = getter() if callable(getter) else []
    names: list[str] = []
    for fc in calls or []:
        name = getattr(fc, "name", None)
        if name:
            names.append(str(name))
    return names


# --------------------------------------------------------------------------- #
# Live status message (posted on drop, edited in-place as the run progresses)
# --------------------------------------------------------------------------- #


def _post_status(
    slack_client: Any,
    channel_id: str,
    text: str,
    thread_ts: Optional[str] = None,
    *,
    blocks: Optional[list] = None,
) -> Optional[str]:
    """Post the initial live-status message and return its ``ts`` (or ``None``).

    Cosmetic-only: a failure here must never abort document processing, so any
    Slack error is logged and swallowed (the run continues silently).
    """
    kwargs: dict = {"channel": channel_id, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    if blocks:
        kwargs["blocks"] = blocks
    try:
        resp = slack_client.chat_postMessage(**kwargs)
    except Exception:  # noqa: BLE001 - status post is cosmetic
        logger.exception("failed to post status message in %s", channel_id)
        return None
    data = resp.data if hasattr(resp, "data") else resp
    if isinstance(data, dict):
        return data.get("ts")
    return None


def _plan_status_blocks(
    stage_state: _StageState,
    source_filename: str,
    channel_id: str,
) -> list:
    """Block Kit accordion for live pipeline progress (plan block or fallback)."""
    label = f"{_env_prefix()}`{source_filename}`"
    return processing_plan_blocks(
        label,
        stages=stage_state.snapshot(),
        channel_id=channel_id,
    )


def _update_status(
    slack_client: Any,
    channel_id: str,
    ts: Optional[str],
    text: str,
    *,
    blocks: Optional[list] = None,
) -> None:
    """Edit the live-status message in place. No-op when ``ts`` is missing.

    Cosmetic-only: a failed ``chat_update`` is logged and swallowed so it can
    never crash the run (real processing errors are raised elsewhere).
    """
    if not ts:
        return
    kwargs: dict = {"channel": channel_id, "ts": ts, "text": text}
    if blocks:
        kwargs["blocks"] = blocks
    try:
        slack_client.chat_update(**kwargs)
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
    hint: str = "",
    replace: bool = False,
    defer_slack_delivery: bool = False,
    batch_mode: bool = False,
    defer_ledger_persist: bool = False,
    status_callback: Optional[Callable[[dict], None]] = None,
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
            _stage_state.mark_failed("understand", "Couldn't read this file")
            _update_status(
                slack_client,
                channel_id,
                status_ts,
                "❌ Couldn't read this file",
                blocks=_plan_status_blocks(_stage_state, source_filename, channel_id),
            )
            _post_message(
                slack_client, channel_id,
                f"Sorry, I couldn't read `{source_filename}` — {rejection_reason}. "
                "Please re-upload a supported document (PDF, PNG, JPG, WEBP, or GIF).",
                thread_ts=thread_ts,
            )
            if batch_mode and status_callback is not None:
                status_callback({
                    "file_label": source_filename,
                    "stage": "Couldn't read this file",
                    "detail": rejection_reason,
                    "status": "failed",
                })
            return {
                "status": "rejected_unreadable",
                "channel_id": channel_id,
                "file_id": file_id,
                "reason": rejection_reason,
            }

        artifact_name = nodes.artifact_name_for(file_id)

        from invoice_processing.extract.invoice_extractor import mime_for

        artifact_mime = mime_for(source_filename)
        if artifact_mime == "application/octet-stream":
            artifact_mime = "application/pdf"

        await runner.artifact_service.save_artifact(
            app_name=app_name,
            user_id=channel_id,
            session_id=session_id,
            filename=artifact_name,
            artifact=types.Part(
                inline_data=types.Blob(data=data, mime_type=artifact_mime)
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
        # Re-extract (ADR-0010): ALWAYS write both keys unconditionally so a
        # normal re-drop of the same file_id (replace=False, hint="") resets any
        # stale ``reextract_replace=True`` / leftover hint that a prior re-extract
        # run wrote into the long-lived per-doc session. Writing only when present
        # caused the HIGH data-loss bug: the state_delta MERGES into existing
        # session state, so omitting the key leaves the old value in place.
        state_delta["review_hint"] = hint or ""
        state_delta["reextract_replace"] = bool(replace)

        interrupt_id: Optional[str] = None
        last_text = ""
        last_stage: Optional[str] = None
        last_node: Optional[str] = None
        understand_output: Optional[str] = None
        try:
            async for event in runner.run_async(
                user_id=channel_id,
                session_id=session_id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text="process this document")]
                ),
                state_delta=state_delta,
            ):
                # Drive the live status off the real event stream: each node tags its
                # events with node_info.path → friendly stage label. Edit on stage
                # transitions and when a new node completes within the same stage.
                node_name = event_node_name(event)
                stage = event_stage_label(event)
                stage_key = event_stage_key(event)
                if node_name is not None and node_name != last_node:
                    last_node = node_name
                    run_state: dict = {}
                    try:
                        session = await runner.session_service.get_session(
                            app_name=app_name, user_id=channel_id, session_id=session_id
                        )
                        if session and getattr(session, "state", None):
                            run_state = dict(session.state)
                    except Exception:  # noqa: BLE001 — cosmetic status only
                        pass
                    node_output = _stage_output_for_completed_node(node_name, run_state)
                    output_stage = _output_stage_for_node(node_name)
                    if output_stage == "understand" and node_output:
                        understand_output = node_output
                    if stage_key is not None:
                        if stage_key != last_stage:
                            last_stage = stage_key
                            handoff_output = None
                            if stage_key == "policy" and understand_output:
                                handoff_output = understand_output
                            elif stage_key != "understand" and node_output and output_stage != "understand":
                                handoff_output = node_output
                            _stage_state.advance(stage_key, output=handoff_output)
                        if node_output and output_stage:
                            _stage_state.set_output(output_stage, node_output)
                    if stage is not None:
                        _update_status(
                            slack_client,
                            channel_id,
                            status_ts,
                            stage,
                            blocks=_plan_status_blocks(
                                _stage_state, source_filename, channel_id
                            ),
                        )
                        if batch_mode and status_callback is not None:
                            # Surface the live stage onto the shared batch plan block
                            # so the user sees per-doc thinking in the placeholder.
                            try:
                                _snapshot = _stage_state.snapshot()
                                current_stage = next(
                                    (s for s in _snapshot if s.get("status") == "in_progress"),
                                    None,
                                )
                                status_callback({
                                    "file_label": source_filename,
                                    "stage": (current_stage or {}).get("title") or stage,
                                    "detail": (current_stage or {}).get("output"),
                                    "status": "in_progress",
                                })
                            except Exception:  # noqa: BLE001 - cosmetic only
                                logger.debug("batch status callback failed", exc_info=True)
                iid = find_interrupt_id(event)
                if iid is not None:
                    interrupt_id = iid
                text = extract_final_text(event)
                if text:
                    last_text = text

        except Exception as exc:  # noqa: BLE001 — surface to Slack, don't kill batch
            logger.exception(
                "document processing failed: file=%s channel=%s",
                file_id,
                channel_id,
            )
            err_short = str(exc).split("\n", maxsplit=1)[0][:200]
            if "503" in err_short or "UNAVAILABLE" in err_short:
                user_msg = (
                    f"Gemini is temporarily overloaded — couldn't finish reading "
                    f"`{source_filename}`. Please try again in a minute."
                )
            else:
                user_msg = (
                    f"Sorry, processing failed for `{source_filename}`: {err_short}"
                )
            _stage_state.mark_failed("understand", err_short)
            _update_status(
                slack_client,
                channel_id,
                status_ts,
                "❌ Processing failed",
                blocks=_plan_status_blocks(_stage_state, source_filename, channel_id),
            )
            _post_message(slack_client, channel_id, user_msg, thread_ts=thread_ts)
            _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
            _add_reaction(slack_client, channel_id, upload_msg_ts, "x")
            if batch_mode and status_callback is not None:
                status_callback({
                    "file_label": source_filename,
                    "stage": "Processing failed",
                    "detail": err_short,
                    "status": "failed",
                })
            return {
                "status": "processing_failed",
                "channel_id": channel_id,
                "file_id": file_id,
                "error": err_short,
            }

        outcome = await _finalize_run_outcome(
            events=[],  # events already drained into interrupt_id / last_text above
            interrupt_id=interrupt_id,
            last_text=last_text,
            runner=runner,
            ledger_store=ledger_store,
            db=db,
            slack_client=slack_client,
            channel_id=channel_id,
            session_id=session_id,
            app_name=app_name,
            user_id=channel_id,
            file_id=file_id,
            thread_ts=thread_ts,
            replace=replace,
            status_ts=status_ts,
            source_filename=source_filename,
            stage_state=_stage_state,
            defer_slack_delivery=defer_slack_delivery,
            batch_mode=batch_mode,
            defer_ledger_persist=defer_ledger_persist,
            client_store=client_store,
        )

        if outcome["status"] == "paused":
            _stage_state.advance("commit")
            _stage_state.set_output("policy", "Waiting for your approval")
            _update_status(
                slack_client,
                channel_id,
                status_ts,
                "⏳ Needs your review",
                blocks=_plan_status_blocks(
                    _stage_state, source_filename, channel_id
                ),
            )
            if batch_mode and status_callback is not None:
                status_callback({
                    "file_label": source_filename,
                    "stage": "Awaiting your review",
                    "detail": "paused for approval",
                    "status": "in_progress",
                })
            return outcome

        # Delivery branch — collapse status to one line (delivery card is the headline).
        append_result = outcome.get("append", {})
        payload = append_result  # fy/software/kind carried on append result
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
                status_callback({
                    "file_label": source_filename,
                    "stage": "Already recorded",
                    "detail": "duplicate of a prior entry",
                    "status": "complete",
                })
            return {"status": "duplicate", "append": append_result}

        terminal = _terminal_status_line(append_result, payload)
        _update_status(
            slack_client,
            channel_id,
            status_ts,
            terminal,
            blocks=_simple_status_blocks(terminal),
        )
        # Swap the 👀 reaction for ✅ on the user's original upload message.
        _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
        _add_reaction(slack_client, channel_id, upload_msg_ts, "white_check_mark")
        if batch_mode and status_callback is not None:
            status_callback({
                "file_label": source_filename,
                "stage": "Added to ledger",
                "detail": terminal,
                "status": "complete",
            })
        return outcome


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


async def _apply_state_delta(
    runner: Any, app_name: str, user_id: str, session_id: str, state_delta: dict
) -> None:
    """Merge ``state_delta`` into the live session by appending a state-only event.

    Used by the chat-lane confirm flow to persist the cleared ``pending_ledger_write``
    list, the idempotency marker, and the refreshed ``ledger_data`` AFTER the
    write tools have run — so the next turn sees the post-write state. Best-effort:
    a persistence failure must not crash the chat lane (the workbook write already
    succeeded).
    """
    if not state_delta:
        return
    try:
        from google.adk.events.event import Event
        from google.adk.events.event_actions import EventActions

        session = await runner.session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        if session is None:
            return
        await runner.session_service.append_event(
            session,
            Event(author="assistant", actions=EventActions(state_delta=state_delta)),
        )
    except Exception:  # noqa: BLE001 — state persistence is best-effort
        logger.exception("failed to persist post-write chat state delta")


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


_DOC_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "doc_type",
    "extraction_path",
    "review_reasons",
    "source_filename",
    "summary_table",
    "normalized_invoice_count",
    "soa_legacy_path",
)


def _coerce_snapshot_fields(state: dict) -> dict:
    """Pick the chat-relevant fields out of a per-document session state.

    Returns a fresh dict with a small, LLM-friendly subset. The
    ``summary_table`` is reduced to its length so the JSON payload stays
    small even for very long documents. Anything missing in the source
    state is omitted from the result (the chat tools handle absent fields
    gracefully).
    """
    if not isinstance(state, dict):
        return {}
    out: dict = {}
    for key in _DOC_SNAPSHOT_FIELDS:
        val = state.get(key)
        if val is None:
            continue
        if key == "summary_table":
            try:
                out["summary_table_size"] = len(val)
            except TypeError:
                continue
            continue
        out[key] = val
    return out


async def _snapshot_doc_sessions(
    runner: Any,
    app_name: str,
    channel_id: str,
    file_ids: list[str],
) -> dict:
    """Read-only snapshot of per-document session state for chat introspection.

    For each ``file_id`` in ``file_ids``, try to load the per-document ADK
    session (``{channel_id}:{file_id}``) and pull out a small subset of
    fields the chat agent can cite. The runner never writes here — it is
    pure introspection data injected into ``state["document_sessions"]``
    so the chat tools stay free of Firestore / ADK session I/O.

    Returns:
        A dict ``{file_id: snapshot_dict, ...}``; missing/unreadable
        files are simply absent from the mapping.
    """
    out: dict = {}
    if not runner or not file_ids:
        return out
    for fid in file_ids:
        if not fid:
            continue
        session_id = f"{channel_id}:{fid}"
        try:
            sess = await runner.session_service.get_session(
                app_name=app_name, user_id=session_id, session_id=session_id
            )
        except Exception:  # noqa: BLE001 - best-effort
            continue
        if not sess or not getattr(sess, "state", None):
            continue
        snap = _coerce_snapshot_fields(sess.state)
        if snap:
            out[fid] = snap
    return out


_DOC_BACKFILL_KEYS: tuple[str, ...] = (
    "delivered",
    "final_status",
    "summary_table",
    "extraction_path",
)


def _build_processing_log_entry(file_id: str, state: dict) -> dict:
    """Build a ``processing_log`` entry from an ADK session state snapshot."""
    from datetime import datetime, timezone

    doc_type = str(
        state.get("doc_type") or state.get("_doc_type") or "invoice"
    ).strip().lower()
    extraction_path = str(
        state.get("extraction_path") or "unknown"
    ).strip().lower()
    summary_table = state.get("summary_table") or []
    try:
        row_count = int(state.get("row_count") or len(summary_table) or 0)
    except (TypeError, ValueError):
        row_count = 0
    return {
        "file_id": file_id,
        "filename": str(
            state.get("source_filename") or state.get("filename") or file_id
        ),
        "doc_type": doc_type,
        "extraction_path": extraction_path,
        "delivered_at": (
            state.get("delivered_at")
            or state.get("finalized_at")
            or datetime.now(timezone.utc).isoformat()
        ),
        "row_count": row_count,
        "fy": str(state.get("fy") or ""),
        "soa_legacy_path": (
            doc_type == "statement_of_account" or extraction_path == "legacy"
        ),
        "backfilled": True,
    }


async def _lazy_backfill_processing_log(
    *,
    client_store: Any,
    client_id: str,
    channel_id: str,
    app_name: str,
    limit: int = 20,
) -> list[dict]:
    """Reconstruct the recent processing log for ``client_id`` from doc sessions.

    Used by ``answer_question`` when the persisted log is empty so the
    chat agent sees historical deliveries without a manual script run.
    Same logic as :mod:`scripts.backfill_processing_log` but capped and
    scoped to the current channel.

    Returns:
        A list of backfilled processing_log entries (possibly empty).
    """
    # The doc runner lives on the chat runner's session service; reuse it
    # when the chat runner's session_service is the same one Firestore
    # session service uses. Otherwise, walk whatever session service the
    # chat runner exposes.
    from accounting_agents.sessions import FirestoreSessionService

    out: list[dict] = []
    try:
        svc = FirestoreSessionService()
        resp = await svc.list_sessions(
            app_name="accounting_agents_document", user_id=channel_id
        )
        sessions = list(getattr(resp, "sessions", resp) or [])
    except Exception:  # noqa: BLE001
        return out
    for sess_meta in sessions[:limit]:
        session_id = (
            sess_meta.get("id")
            if isinstance(sess_meta, dict)
            else getattr(sess_meta, "id", None)
        )
        if not session_id or not str(session_id).startswith(f"{channel_id}:"):
            continue
        file_id = str(session_id).split(":", 1)[-1]
        try:
            sess = await svc.get_session(
                app_name="accounting_agents_document",
                user_id=channel_id,
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001
            continue
        state = getattr(sess, "state", None) or {}
        if not state or not any(k in state for k in _DOC_BACKFILL_KEYS):
            continue
        entry = _build_processing_log_entry(file_id, state)
        try:
            client_store.append_processing_log(
                client_id=client_id, file_id=file_id, entry=entry
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "lazy backfill persist failed for file_id=%s", file_id
            )
        out.append(entry)
    return out


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


_DOC_TYPE_LABELS: dict[str, str] = {
    "invoice": "Invoice",
    "purchase": "Purchase invoice",
    "sales": "Sales invoice",
    "receipt": "Receipt",
    "telco": "Telco bill",
    "utility": "Utility bill",
    "bank_statement": "Bank statement",
    "statement_of_account": "Statement of account",
    "other": "Document",
}


def _stage_output_for_completed_node(node_name: str, state: dict) -> Optional[str]:
    """Build a short plan-block output line after a graph node finishes."""
    if not node_name or not state:
        return None

    doc_type = (state.get(nodes.DOC_TYPE_KEY) or "invoice").strip().lower()
    direction = (state.get(nodes.DIRECTION_KEY) or "purchase").strip().lower()
    invs = state.get(nodes.NORMALIZED_KEY) or []

    if node_name == "classify_node":
        label = _DOC_TYPE_LABELS.get(doc_type, doc_type.replace("_", " ").title())
        if doc_type not in ("bank_statement", "statement_of_account", "other"):
            label = f"{direction.title()} {label.lower()}" if direction else label
        return label

    if node_name == "extract_invoice_document_node":
        if not invs:
            return None
        first = invs[0] if isinstance(invs[0], dict) else {}
        vendor = _vendor_from_inv_dict(first) or "Unknown vendor"
        inv_no = (first.get("invoice_number") or "").strip()
        total = first.get("doc_total")
        cur = (first.get("currency") or "?").strip().upper()
        lines = first.get("lines") or []
        n_lines = len(lines)
        parts = [vendor]
        if inv_no:
            parts.append(f"#{inv_no[:24]}")
        if isinstance(total, (int, float)):
            parts.append(f"{cur} {total:,.2f}")
        if n_lines:
            parts.append(f"{n_lines} line{'s' if n_lines != 1 else ''}")
        return " · ".join(parts)

    if node_name == "extract_bank_node":
        banks = state.get(nodes.BANK_STATEMENTS_KEY) or []
        if not banks:
            return "Bank statement"
        first = banks[0] if isinstance(banks[0], dict) else {}
        bank = (first.get("bank_name") or first.get("bank") or "Bank").strip()
        txns = first.get("transactions") or []
        return f"{bank} · {len(txns)} transaction{'s' if len(txns) != 1 else ''}"

    if node_name == "categorize_node":
        if not invs:
            return None
        first = invs[0] if isinstance(invs[0], dict) else {}
        codes = {
            (ln.get("account_code") or "").strip()
            for ln in (first.get("lines") or [])
            if isinstance(ln, dict) and (ln.get("account_code") or "").strip()
        }
        if not codes:
            return "Chart of accounts matched"
        if len(codes) == 1:
            return f"Account {next(iter(codes))}"
        return f"{len(codes)} account codes"

    if node_name in ("tax_node", "approval_gate"):
        if not invs:
            return None
        first = invs[0] if isinstance(invs[0], dict) else {}
        taxes = {
            (ln.get("tax_treatment") or ln.get("tax_code") or "").strip()
            for ln in (first.get("lines") or [])
            if isinstance(ln, dict)
        }
        taxes = {t for t in taxes if t}
        tax_label = next(iter(taxes)) if len(taxes) == 1 else (
            f"{len(taxes)} tax codes" if taxes else "Tax applied"
        )
        if first.get("reconciled", True):
            return f"{tax_label} · reconciled"
        note = (first.get("reconcile_note") or "needs review").strip()
        if len(note) > 60:
            note = note[:59] + "…"
        return f"{tax_label} · {note}"

    if node_name in ("consolidate_node", "deliver_node"):
        payload = state.get(nodes.LEDGER_ROWS_KEY) or {}
        fy = payload.get("fy")
        kind = payload.get("kind") or doc_type
        batches = payload.get("batches") or []
        n_rows = sum(len(b.get("rows") or []) for b in batches)
        if fy and n_rows:
            return f"FY{fy} · {n_rows} row{'s' if n_rows != 1 else ''} · {kind}"
        if n_rows:
            return f"{n_rows} row{'s' if n_rows != 1 else ''} ready"
        return None

    return None


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
            # Lever 4 (ADR-0017 §6) — close the 4c vector: a corrected doc
            # shape is no longer trusted.  Reset the doc_type:vendor key so
            # the Engine must see clean approvals again before suppressing.
            doc_type = (state.get(nodes.DOC_TYPE_KEY) or "invoice").strip().lower()
            try:
                client_store.reset_familiarity(
                    client_id=client_id,
                    doc_type=doc_type,
                    vendor=vendor,
                )
            except Exception:  # noqa: BLE001 — reset is best-effort
                logger.debug(
                    "familiarity reset failed for client=%s doc_type=%s vendor=%s",
                    client_id, doc_type, vendor,
                )


def _record_familiarity_from_state(client_store, state: dict) -> None:
    """Record a familiarity increment for the doc described by ``state``.

    Lever 4 (ADR-0017 §6): called on the confident path (clean no-pause run)
    and on un-edited HITL approval.  Reads ``client_id``, ``doc_type``, and
    the dominant vendor from the normalized invoice in ``state`` and calls
    ``client_store.record_familiarity``.  No-ops cleanly when client_id or
    doc_type is absent, or when the store does not support the method.
    """
    if not isinstance(state, dict):
        return
    client_id = state.get("client_id")
    doc_type = (state.get(nodes.DOC_TYPE_KEY) or "").strip().lower()
    if not client_id or not doc_type:
        return
    invs = state.get(nodes.NORMALIZED_KEY) or []
    vendor: Optional[str] = None
    if invs:
        first = invs[0] if isinstance(invs[0], dict) else {}
        vendor = _vendor_from_inv_dict(first)
    direction = (state.get(nodes.DIRECTION_KEY) or "purchase").strip().lower()
    try:
        client_store.record_familiarity(
            client_id=client_id,
            doc_type=doc_type,
            vendor=vendor or None,
            direction=direction,
        )
    except Exception:  # noqa: BLE001 — familiarity record is best-effort
        logger.debug(
            "record_familiarity failed for client=%s doc_type=%s", client_id, doc_type
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


async def _finalize_run_outcome(
    *,
    events: list,
    interrupt_id: Optional[str],
    last_text: str,
    runner: Any,
    ledger_store: SlackLedgerStore,
    db: Any,
    slack_client: Any,
    channel_id: str,
    session_id: str,
    app_name: str,
    user_id: str,
    file_id: str,
    thread_ts: Optional[str] = None,
    replace: bool = False,
    status_ts: Optional[str] = None,
    source_filename: Optional[str] = None,
    stage_state: Optional[_StageState] = None,
    defer_slack_delivery: bool = False,
    batch_mode: bool = False,
    defer_ledger_persist: bool = False,
    client_store=None,
) -> dict:
    """Shared post-run tail: post the right card or deliver, based on the event stream.

    Called from both ``process_file_event`` (fresh run) and
    ``handle_review_action`` (resumed run).  In the fresh-run caller the
    generator has already been drained into ``interrupt_id`` / ``last_text``
    before calling this helper; for the resumed caller ``events`` is the list
    returned by ``resume_session`` and we scan it here.

    Two branches:
    * **Interrupt detected** — a ``find_interrupt_id`` hit in ``events`` (or the
      pre-computed ``interrupt_id`` argument from the fresh-run path).  If the
      id ends with ``:review`` → post the review card + write the review
      interrupt doc.  Otherwise → post the approval card + write the approval
      interrupt doc.  Returns ``{"status": "paused", "op_id": ..., "message_ts": ...}``.
    * **No interrupt** — the run completed cleanly.  Call ``persist_and_deliver``
      and return ``{"status": "delivered"|"duplicate", "append": ...}``.

    ``file_id`` is the Slack file id stored in every interrupt doc so the
    resume handler can re-download the PDF when needed.
    """
    # For the resumed-run caller (handle_review_action), scan the events list
    # for a new interrupt.  For the fresh-run caller, interrupt_id is pre-set
    # and events is empty — so the scan is a no-op.
    for ev in events:
        iid = find_interrupt_id(ev)
        if iid is not None:
            interrupt_id = iid
        txt = extract_final_text(ev)
        if txt:
            last_text = txt

    if interrupt_id is not None:
        # Read the paused session state once; both card paths need it.
        paused_state = await _read_session_state(
            runner, app_name,
            {"user_id": user_id, "session_id": session_id},
        )

        # Wire the understand-extract summary table into the reviewer-facing
        # thread before the approval/review card lands, so the human sees
        # Gemini's category/details interpretation alongside the Approve/Edit
        # decision. This was the missing terminal/debug visibility for the
        # "why is this booked as Sales?" complaints — previously
        # ``_post_summary_table`` was defined but never called.
        summary_table = paused_state.get(nodes.LEDGER_SUMMARY_TABLE_KEY) or []
        if summary_table:
            try:
                _post_summary_table(
                    slack_client, channel_id, summary_table,
                    thread_ts=thread_ts,
                )
            except Exception:  # noqa: BLE001 - cosmetic; never break the gate
                logger.debug("summary table post failed (non-fatal)", exc_info=True)

        if interrupt_id.endswith(":review"):
            # Mid-flow extract-review interrupt from ``review_extraction_node``.
            question = paused_state.get("review_question") or last_text or ""
            reasons: list = paused_state.get(nodes.REVIEW_REASON_KEY) or []
            posted = _post_review_card(
                slack_client, channel_id, question, interrupt_id,
                reasons, thread_ts=thread_ts,
            )
            write_interrupt(
                db,
                interrupt_id,
                session_id=session_id,
                channel_id=channel_id,
                slack_file_id=file_id,
                message_ts=posted,
                user_id=user_id,
                extra={"kind": "review", "question": question, "reasons": reasons},
            )
        else:
            # Terminal approval-gate interrupt: Approve/Edit/Reject card.
            summary = await _read_interrupt_summary(
                runner, app_name, user_id, session_id, last_text
            )
            doc_label = _doc_label_from_state(paused_state)
            posted = _post_approval_card(
                slack_client, channel_id, summary, interrupt_id,
                thread_ts=thread_ts, doc_label=doc_label,
            )
            extra: dict = {"summary": summary, "doc_label": doc_label}
            if thread_ts:
                extra["thread_ts"] = thread_ts
            if status_ts:
                extra["status_ts"] = status_ts
            if source_filename:
                extra["source_filename"] = source_filename
            write_interrupt(
                db,
                interrupt_id,
                session_id=session_id,
                channel_id=channel_id,
                slack_file_id=file_id,
                message_ts=posted,
                user_id=user_id,
                extra=extra,
            )
        return {"status": "paused", "op_id": interrupt_id, "message_ts": posted}

    # No interrupt — run completed cleanly; persist and deliver.
    append_result = await persist_and_deliver(
        runner=runner,
        ledger_store=ledger_store,
        slack_client=slack_client,
        channel_id=channel_id,
        session_id=session_id,
        app_name=app_name,
        user_id=user_id,
        thread_ts=thread_ts,
        replace=replace,
        defer_slack_delivery=defer_slack_delivery,
        batch_mode=batch_mode,
        defer_ledger_persist=defer_ledger_persist,
        client_store=client_store or _DEFAULT_CLIENT_STORE,
    )
    status = "duplicate" if append_result.get("all_deduped") else "delivered"

    # Step 8 — proactive auto-hint: when the extract reviewer FIRED (non-empty
    # REVIEW_REASON_KEY) on this run but it was filed without ever pausing the
    # user (verdict != CLARIFY — a CLARIFY already surfaced the mid-flow review
    # card and engaged them), offer a re-extract AFTER delivery. A clean
    # happy-path doc writes REVIEW_VERDICT_OK with NO reasons → posts nothing,
    # so the offer is rare. Bank docs don't run the reviewer → no reasons → no
    # offer. Best-effort: a read/post failure must never break the delivery
    # return value.
    try:
        delivered_state = await _read_session_state(
            runner, app_name,
            {"user_id": user_id, "session_id": session_id},
        )
        # Lever 4 (ADR-0017 §6): confident-path delivery → record familiarity
        # so the Engine learns this doc shape is trusted for this client.
        _record_familiarity_from_state(client_store or _DEFAULT_CLIENT_STORE, delivered_state)

        reasons = delivered_state.get(nodes.REVIEW_REASON_KEY) or []
        verdict = delivered_state.get(nodes.REVIEW_VERDICT_KEY)
        if reasons and verdict != nodes.REVIEW_VERDICT_CLARIFY:
            _post_proactive_redo_card(
                slack_client, channel_id, file_id, reasons, thread_ts=thread_ts,
            )
    except Exception:  # noqa: BLE001 - the proactive offer is non-critical
        logger.debug("proactive redo offer skipped for file %s", file_id)

    return {"status": status, "append": append_result}


def _post_proactive_redo_card(
    slack_client: Any, channel_id: str, file_id: str, reasons: list,
    thread_ts=None,
) -> Optional[str]:
    """Post the Step-8 proactive re-extract offer (threaded under the delivery)."""
    kwargs = {
        "channel": channel_id,
        "blocks": proactive_redo_blocks(file_id, reasons, channel_id=channel_id),
        "text": "This document looked off — want me to re-read it?",
    }
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = slack_client.chat_postMessage(**kwargs)
    data = resp.data if hasattr(resp, "data") else resp
    if isinstance(data, dict):
        return data.get("ts")
    return None


def _post_summary_table(
    slack_client: Any,
    channel_id: str,
    summary_table: list,
    thread_ts=None,
) -> Optional[str]:
    """Post Drive-style summary table when understand-extract populated it."""
    if not summary_table:
        return None
    kwargs = {
        "channel": channel_id,
        "blocks": summary_table_blocks(summary_table, channel_id=channel_id),
        "text": "Document summary",
    }
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = slack_client.chat_postMessage(**kwargs)
    data = resp.data if hasattr(resp, "data") else resp
    if isinstance(data, dict):
        return data.get("ts")
    return None


def _post_approval_card(
    slack_client: Any, channel_id: str, summary: str, op_id: str, thread_ts=None,
    doc_label: Optional[str] = None,
) -> Optional[str]:
    kwargs = {
        "channel": channel_id,
        "blocks": approval_card_blocks(summary, op_id, doc_label=doc_label, channel_id=channel_id),
        "text": "Review needed before adding to the ledger.",
    }
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = slack_client.chat_postMessage(**kwargs)
    data = resp.data if hasattr(resp, "data") else resp
    if isinstance(data, dict):
        return data.get("ts")
    return None


def _post_review_card(
    slack_client: Any,
    channel_id: str,
    question: str,
    op_id: str,
    reasons: list,
    thread_ts=None,
) -> Optional[str]:
    """Post the mid-flow review card and return its Slack ``ts``."""
    kwargs = {
        "channel": channel_id,
        "blocks": review_card_blocks(question, op_id, reasons, channel_id=channel_id),
        "text": "Extraction needs your input before continuing.",
    }
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = slack_client.chat_postMessage(**kwargs)
    data = resp.data if hasattr(resp, "data") else resp
    if isinstance(data, dict):
        return data.get("ts")
    return None


def _update_review_card(
    slack_client: Any, interrupt: dict, question: str, action: str
) -> None:
    """Replace the review card with the resolved-outcome block."""
    ts = interrupt.get("message_ts")
    channel_id = interrupt.get("channel_id")
    if not ts or not channel_id:
        return
    try:
        slack_client.chat_update(
            channel=channel_id,
            ts=ts,
            blocks=review_outcome_blocks(question, action),
            text=f"Review resolved: {action}.",
        )
    except Exception:  # noqa: BLE001 - card update is cosmetic
        logger.exception("failed to update review card for %s", channel_id)


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
    thread_ts = interrupt.get("thread_ts") or None

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
            thread_ts=thread_ts,
            client_store=_DEFAULT_CLIENT_STORE,
        )
    else:
        update_interrupt_status(db, op_id, "rejected")
        _post_message(slack_client, channel_id, "Document rejected — nothing was added to the ledger.", thread_ts=thread_ts)

    # Lever 4 (ADR-0017 §6): on an UN-EDITED approval, record familiarity so
    # the Engine learns this doc shape is trusted for this client.  The edit
    # modal emits decision=="edit" — do NOT record familiarity there (a shape
    # that required edits is not yet trusted).
    if decision == "approve":
        try:
            approved_state = await _read_session_state(
                runner, app_name,
                {"user_id": user_id, "session_id": session_id},
            )
            _record_familiarity_from_state(_DEFAULT_CLIENT_STORE, approved_state)
        except Exception:  # noqa: BLE001 — familiarity record is best-effort
            logger.debug("familiarity record failed after approval for op_id %s", op_id)

    _update_card(slack_client, interrupt, summary, decision)

    status_ts = interrupt.get("status_ts")
    source_filename = interrupt.get("source_filename") or "document"
    if status_ts:
        stage_state = _StageState()
        stage_state.mark_complete(
            output="Approved" if decision == "approve" else (
                "Rejected" if decision == "reject" else "Updated"
            ),
        )
        terminal = "✅ Processed" if decision == "approve" else (
            "❌ Rejected" if decision == "reject" else "✅ Processed"
        )
        try:
            _update_status(
                slack_client,
                channel_id,
                status_ts,
                terminal,
                blocks=_plan_status_blocks(
                    stage_state, source_filename, channel_id
                ),
            )
        except Exception:  # noqa: BLE001 — cosmetic
            logger.debug("failed to finalize plan status after approval", exc_info=True)

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
# Review action handlers (mid-flow extract-review HITL)
# --------------------------------------------------------------------------- #


async def handle_review_action(
    *,
    runner: Any,
    ledger_store: SlackLedgerStore,
    db: Any,
    slack_client: Any,
    op_id: str,
    action: str,
    app_name: str,
    hint: Optional[str] = None,
) -> dict:
    """Resume a paused ``review_extraction_node`` interrupt with the human's decision.

    Idempotent: guarded by the ``processed/{op_id}`` marker so a double-click
    resumes at most once.  After resume the workflow falls through to
    ``categorize_node`` and (if the extraction is non-empty) to the terminal
    ``approval_gate``.  For ``confirm_as_is`` / ``reextract_as`` we hand the
    resumed events to :func:`_finalize_run_outcome`, which posts the terminal
    approval card if the gate paused again, or persists + delivers if the gate
    auto-approved and the run completed — exactly like a fresh run.  Without
    this the document would silently stall (re-pause never surfaced) or be lost
    (auto-approve never delivered).

    Single-shot review (MEDIUM-2): the ``:review`` op_id is derived
    deterministically (``{approval_id}:review``) and ``mark_processed`` after
    one resume, so a second ``:review`` escalation for the SAME document would
    collide with the processed marker.  This is safe today only because the node
    applies the decision on resume and falls straight through to categorize
    WITHOUT re-running the reviewer loop — there is no second ``:review`` pause
    per document.

    Args:
        op_id:   The ``:review`` interrupt id.
        action:  One of ``"reextract_as"``, ``"confirm_as_is"``, or ``"reject"``.
        hint:    Optional free-text hint; only meaningful for ``"reextract_as"``.
    """
    if is_processed(db, op_id):
        logger.info("review action for %s already processed; ignoring.", op_id)
        return {"status": "already_processed", "op_id": op_id}

    interrupt = read_interrupt(db, op_id)
    if interrupt is None:
        logger.warning("no interrupt doc for op_id %s; cannot resume review.", op_id)
        return {"status": "missing_interrupt", "op_id": op_id}

    channel_id = interrupt["channel_id"]
    session_id = interrupt["session_id"]
    question = interrupt.get("question") or ""

    decision = ReviewClarifyDecision(action=action, hint=hint if action == "reextract_as" else None)
    events = await resume_session(runner, db, op_id, decision)

    _update_review_card(slack_client, interrupt, question, action)

    if action == "reject":
        update_interrupt_status(db, op_id, "rejected")
        _post_message(slack_client, channel_id, "Document rejected — nothing was added to the ledger.")
        return {"status": "resumed", "op_id": op_id, "events": len(events)}

    # confirm_as_is / reextract_as: the resumed run flowed through categorize/tax
    # to the terminal approval_gate.  Finalize that outcome exactly like a fresh
    # run — post the terminal approval card on a re-pause, or persist + deliver
    # on a clean auto-approve.  Pass the SAME session/user the run was keyed
    # under (as resume_session resolved them from the interrupt doc).
    outcome = await _finalize_run_outcome(
        events=events,
        interrupt_id=None,
        last_text="",
        runner=runner,
        ledger_store=ledger_store,
        db=db,
        slack_client=slack_client,
        channel_id=channel_id,
        session_id=session_id,
        app_name=app_name,
        user_id=interrupt.get("user_id") or session_id,
        file_id=interrupt.get("slack_file_id") or "",
        thread_ts=interrupt.get("thread_ts"),
        client_store=_DEFAULT_CLIENT_STORE,
    )
    return {"status": "resumed", "op_id": op_id, "outcome": outcome, "events": len(events)}


# --------------------------------------------------------------------------- #
# Text-question → Q&A path
# --------------------------------------------------------------------------- #


async def _handle_chat_turn(
    *,
    chat_runner: Any,
    ledger_store: SlackLedgerStore,
    slack_client: Any,
    channel_id: str,
    question: str,
    client_store,
    message_ts: Optional[str],
    thread_ts: Optional[str],
    raw_thread_ts: Optional[str],
    doc_runner: Any,
    db: Any,
) -> None:
    """Route one user text turn to the ADK chat assistant (single code path)."""
    thinking_ts = thread_ts or raw_thread_ts or message_ts
    try:
        await answer_question(
            runner=chat_runner,
            ledger_store=ledger_store,
            slack_client=slack_client,
            channel_id=channel_id,
            question=question,
            app_name=chat_runner.app_name,
            client_store=client_store,
            message_ts=message_ts,
            thread_ts=thread_ts,
            raw_thread_ts=raw_thread_ts,
            doc_runner=doc_runner,
            db=db,
        )
    except Exception:
        logger.exception("answer_question failed for channel %s", channel_id)
        if _chat_ux_enabled():
            _clear_chat_thinking(slack_client, channel_id, thinking_ts)
            _remove_reaction(slack_client, channel_id, message_ts, "eyes")
        _post_message(
            slack_client,
            channel_id,
            "Sorry — I hit an error answering that. Check the bot logs, or try "
            "dropping your PDFs directly in the channel.",
            thread_ts=thread_ts,
        )


def _verify_row_signature(
    ledger_store: SlackLedgerStore,
    slack_client: Any,
    client_id: str,
    fy: str,
    channel_id: str,
    sheet: str,
    row: int,
    expected_sig: str,
) -> Optional[str]:
    """Read the live workbook row and compare it against the proposal-time signature.

    Returns ``None`` when the content matches (safe to write), or a human-readable
    reason string when the content has shifted — row deletion upstream shifts all
    subsequent row numbers, so writing to the original row number would corrupt a
    different line.  Called synchronously (inside ``asyncio.to_thread``).
    """
    from accounting_agents.assistant import _row_signature  # local import avoids circular at module level

    try:
        all_rows = ledger_store.read_rows(
            client_id=client_id, fy=fy,
            slack_client=slack_client, channel_id=channel_id,
        )
    except Exception:  # noqa: BLE001
        return "Could not re-read the workbook to verify the row — write aborted."

    # Find the row at (sheet, row) coordinate.
    live_row = next(
        (r for r in all_rows if r.get("_sheet") == sheet and r.get("_row") == row),
        None,
    )
    if live_row is None:
        return (
            f"Row {row} on sheet {sheet!r} no longer exists — it may have been "
            "deleted or shifted since you saw the proposal. Please look up the "
            "row again with `lookup_row` and re-propose the change."
        )

    live_sig = _row_signature(live_row)
    if live_sig != expected_sig:
        live_desc = live_row.get("Description") or "(unknown)"
        return (
            f"The content of {sheet} row {row} changed since you approved the edit "
            f"(now: {live_desc!r}). The write was aborted to prevent corrupting "
            "the wrong line. Please use `lookup_row` to find the row again and "
            "re-propose the change."
        )
    return None


async def _execute_pending_writes(
    *,
    state: dict,
    ledger_store: SlackLedgerStore,
    slack_client: Any,
    channel_id: str,
    client_id: str,
    fy: str,
    session_id: str,
    fc_id: Optional[str],
    thread_ts: Optional[str] = None,
) -> bool:
    """Drain ``state["pending_ledger_write"]`` → mutate the workbook + post + audit.

    Executes each confirmed write spec (``amend`` / ``remove``) against the FY
    workbook in a thread pool (the blocking download/upload, mirroring
    :func:`persist_and_deliver`). Posts a confirmation message, appends an audit
    log line, and clears the pending list. Returns ``True`` when at least one
    write was committed (so the caller refreshes ``ledger_data``).

    Idempotency: guarded by an in-state ``committed_confirmations`` marker keyed
    by the answering ``fc_id`` so a double "yes" commits the batch exactly once.

    Replay-safety (HIGH-2): BEFORE each write, re-reads the live workbook row and
    verifies its signature matches the one captured at Turn-1. A mismatch means
    the row shifted (upstream deletion) or was edited since the proposal — the
    write is refused and the user is told to re-propose rather than silently
    corrupting a different row.
    """
    pending = state.get(PENDING_WRITE_KEY)
    if not isinstance(pending, list) or not pending:
        return False

    committed = state.get("committed_confirmations")
    if not isinstance(committed, list):
        committed = []
    if fc_id and fc_id in committed:
        # Already applied for this confirmation — clear and skip (double "yes").
        state[PENDING_WRITE_KEY] = []
        return False

    any_committed = False
    for spec in pending:
        if not isinstance(spec, dict):
            continue
        op = spec.get("op")
        sheet = spec.get("sheet")
        row = spec.get("row")
        expected_sig = spec.get("row_signature")

        # ---- Signature check (HIGH-2) — verify row before mutating ----
        if expected_sig and sheet and row:
            sig_error = await asyncio.to_thread(
                _verify_row_signature,
                ledger_store, slack_client,
                client_id, fy, channel_id,
                sheet, row, expected_sig,
            )
            if sig_error:
                logger.warning(
                    "CHAT_WRITE_ABORTED sig_mismatch op=%s sheet=%s row=%s "
                    "session=%s client=%s: %s",
                    op, sheet, row, session_id, client_id, sig_error,
                )
                _post_message(
                    slack_client, channel_id,
                    f"⚠️ {sig_error}",
                    thread_ts=thread_ts,
                )
                continue  # skip this spec; don't mark any_committed

        try:
            if op == "amend":
                result = await asyncio.to_thread(
                    ledger_store.amend_row,
                    client_id=client_id,
                    fy=fy,
                    slack_client=slack_client,
                    channel_id=channel_id,
                    sheet=sheet,
                    row=row,
                    updates=spec.get("updates") or {},
                )
                changes = ", ".join(
                    f"{col}: {result['before'].get(col)!r} → {new!r}"
                    for col, new in (result.get("after") or {}).items()
                )
                _post_message(
                    slack_client, channel_id,
                    f"✅ Updated {sheet} row {row} — {changes}.",
                    thread_ts=thread_ts,
                )
                logger.info(
                    "CHAT_WRITE_AUDIT op=amend session=%s channel=%s client=%s fy=%s "
                    "sheet=%s row=%s before=%r after=%r tax=%r",
                    session_id, channel_id, client_id, fy, sheet, row,
                    result.get("before"), result.get("after"),
                    spec.get("tax_treatment"),
                )
                any_committed = True
            elif op == "remove":
                result = await asyncio.to_thread(
                    ledger_store.remove_row,
                    client_id=client_id,
                    fy=fy,
                    slack_client=slack_client,
                    channel_id=channel_id,
                    sheet=sheet,
                    row=row,
                )
                _post_message(
                    slack_client, channel_id,
                    f"🗑️ Removed {sheet} row {row} from your ledger.",
                    thread_ts=thread_ts,
                )
                logger.info(
                    "CHAT_WRITE_AUDIT op=remove session=%s channel=%s client=%s fy=%s "
                    "sheet=%s row=%s removed=%r",
                    session_id, channel_id, client_id, fy, sheet, row,
                    result.get("removed"),
                )
                any_committed = True
            elif op == "replace_month":
                import calendar
                year = spec.get("year")
                month_num = spec.get("month")
                try:
                    result = await asyncio.to_thread(
                        ledger_store.remove_rows_for_month,
                        client_id,
                        fy,
                        slack_client,
                        channel_id,
                        year=year,
                        month=month_num,
                    )
                    month_name = calendar.month_name[month_num] if month_num else "?"
                    total_removed = len(result.get("removed") or [])
                    _post_message(
                        slack_client, channel_id,
                        f"Cleared {total_removed} rows for {month_name} {year} — "
                        "re-drop those documents to re-record them.",
                        thread_ts=thread_ts,
                    )
                    logger.info(
                        "CHAT_WRITE_AUDIT op=replace_month session=%s channel=%s "
                        "client=%s fy=%s year=%s month=%s removed=%d purged_keys=%r",
                        session_id, channel_id, client_id, fy,
                        year, month_num, total_removed,
                        result.get("purged_keys"),
                    )
                    any_committed = True
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "replace_month failed: year=%s month=%s channel=%s",
                        year, month_num, channel_id,
                    )
                    _post_message(
                        slack_client, channel_id,
                        "⚠️ I couldn't clear that month from the ledger. Nothing was modified.",
                        thread_ts=thread_ts,
                    )
                continue  # skip the outer except below — already handled

        except Exception:  # noqa: BLE001 — a failed write must not crash the lane
            logger.exception(
                "chat write failed: op=%s sheet=%s row=%s channel=%s",
                op, sheet, row, channel_id,
            )
            _post_message(
                slack_client, channel_id,
                f"⚠️ I couldn't apply that change to {sheet} row {row}. "
                "Nothing was modified.",
                thread_ts=thread_ts,
            )

    # Mark this confirmation committed + clear the pending list (idempotency).
    if fc_id:
        committed.append(fc_id)
        state["committed_confirmations"] = committed
    state[PENDING_WRITE_KEY] = []
    return any_committed


async def _execute_pending_reextract(
    specs: list,
    *,
    doc_runner: Any,
    ledger_store: SlackLedgerStore,
    db: Any,
    slack_client: Any,
    channel_id: str,
    app_name: str,
    client_store=None,
    thread_ts: Optional[str] = None,
) -> bool:
    """Drain ``state["pending_reextract"]`` → re-run each document through the pipeline.

    For each spec (``{"op": "reextract", "file_id", "hints"}``) re-run the FULL
    document pipeline via :func:`process_file_event` with the hint seeded and
    ``replace=True`` (ADR-0010): the corrected read flows through the same
    Approve / Edit / Reject card and its rows replace the old ones by reconstructed
    identity. ``process_file_event`` posts its own status / card / delivery, so we
    just let it run and only add a note when the identity changed (replaced 0 rows
    yet still recorded — the user must clear the stale rows by month).

    Idempotency: a per-``file_id:hint`` marker on the doc-runner instance guards a
    double "yes" so the same re-extract is not run twice. Returns ``True`` when at
    least one re-extract was dispatched.

    Runs on the DOCUMENT runner (the graph) — the chat runner cannot drive the
    doc pipeline (ADR-0010); the caller injects ``doc_runner``.
    """
    if not isinstance(specs, list) or not specs:
        return False

    # Per-run-instance idempotency marker: a double "yes" re-drains the same list,
    # so skip any (file_id, hints) pair already dispatched in this process.
    seen = getattr(doc_runner, "_reextract_seen", None)
    if not isinstance(seen, set):
        seen = set()
        try:
            doc_runner._reextract_seen = seen
        except Exception:  # noqa: BLE001 — a read-only fake is still fine; dedup degrades
            pass

    dispatched = False
    for spec in specs:
        if not isinstance(spec, dict) or spec.get("op") != "reextract":
            continue
        file_id = str(spec.get("file_id") or "").strip()
        hints = str(spec.get("hints") or "").strip()
        if not file_id or not hints:
            continue

        key = f"{file_id}:{hints}"
        if key in seen:
            logger.info(
                "re_extract already dispatched (file=%s) — skipping double-run.",
                file_id,
            )
            continue
        seen.add(key)

        try:
            result = await process_file_event(
                runner=doc_runner,
                ledger_store=ledger_store,
                db=db,
                slack_client=slack_client,
                channel_id=channel_id,
                file_id=file_id,
                app_name=app_name,
                download_fn=download_pdf_bytes,
                source_filename=f"re-extract-{file_id}.pdf",
                hint=hints,
                replace=True,
                client_store=client_store,
                thread_ts=thread_ts,
            )
        except Exception:  # noqa: BLE001 — a failed re-extract must not crash the lane
            logger.exception(
                "re_extract failed: file=%s channel=%s", file_id, channel_id,
            )
            _post_message(
                slack_client, channel_id,
                f"⚠️ I couldn't re-read file {file_id}. Nothing was changed.",
                thread_ts=thread_ts,
            )
            continue

        dispatched = True
        status = (result or {}).get("status")

        # Identity-change check (ADR-0010 §3): when the re-read matched 0 old
        # rows to replace, the document's identity changed (e.g. a credit note).
        # This shows up as either:
        #   (a) status="delivered" with appended>0, replaced=0 — added rows but
        #       couldn't remove the old ones; or
        #   (b) status="duplicate" (all_deduped) — the corrected doc_key is
        #       already in seen_doc_keys AND replaced=0, meaning the stale rows
        #       from the original identity are still in the sheet uncleaned.
        # In both cases point the user at the month-level primitive.
        append_result = (result or {}).get("append", {}) or {}
        replace_counts = append_result.get("batch_replace_counts") or []
        total_replaced = sum(int(b.get("replaced") or 0) for b in replace_counts)
        total_appended = sum(int(b.get("appended") or 0) for b in replace_counts)
        identity_changed = total_replaced == 0 and (
            (status == "delivered" and total_appended > 0)
            or status == "duplicate"
        )
        if identity_changed:
            _post_message(
                slack_client, channel_id,
                "Heads up: the re-read changed the document's identity, so I "
                "added the corrected version but couldn't auto-remove the old "
                "rows. Use `clear <month>` (replace_recorded_month) to drop the "
                "stale rows.",
                thread_ts=thread_ts,
            )

        logger.info(
            "CHAT_REEXTRACT_AUDIT channel=%s file=%s hints=%r status=%s "
            "replaced=%d appended=%d",
            channel_id, file_id, hints, status, total_replaced, total_appended,
        )

    return dispatched


def _dominant_fy_from_processing_log(processing_log: list) -> str | None:
    """Return the FY label that appears most often in the processing log."""
    from collections import Counter

    counts: Counter[str] = Counter()
    for entry in processing_log or []:
        if not isinstance(entry, dict):
            continue
        fy = str(entry.get("fy") or "").strip()
        if fy:
            counts[fy] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _fy_hint_from_question(question: str, processing_log: list) -> str | None:
    """If the user's message names a file/invoice, return its FY from the log."""
    from accounting_agents.assistant_tools._helpers import filename_matches_query
    import re

    q = (question or "").strip()
    if not q:
        return None
    tokens = re.findall(r"[\w][\w-]*", q)
    for entry in processing_log or []:
        if not isinstance(entry, dict):
            continue
        fn = str(entry.get("filename") or "")
        fy = str(entry.get("fy") or "").strip()
        if not fn or not fy:
            continue
        if filename_matches_query(q, fn):
            return fy
        for tok in tokens:
            if len(tok) >= 3 and filename_matches_query(tok, fn):
                return fy
    return None


def _invoice_ids_from_batches(batches: list[dict]) -> list[str]:
    """Collect invoice numbers from exporter row dicts in delivery batches."""
    ids: list[str] = []
    for batch in batches or []:
        for row in batch.get("rows") or []:
            if not isinstance(row, dict):
                continue
            inv = (
                row.get("*InvoiceNumber")
                or row.get("Invoice Number")
                or row.get("Reference")
            )
            if inv:
                token = str(inv).strip()
                if token and token not in ids:
                    ids.append(token)
    return ids


def _data_table_cell_text(cell: Any) -> str:
    if isinstance(cell, dict):
        return str(cell.get("text") or "").strip()
    return str(cell or "").strip()


def _map_delivery_table_headers(headers: list[str]) -> dict[str, int]:
    """Map logical preview keys to column indices in a delivery data_table."""
    mapping: dict[str, int] = {}
    for idx, header in enumerate(headers):
        hl = header.lower().strip()
        if hl in {"invoice #", "invoice number", "invoice", "inv #"}:
            mapping.setdefault("invoice_id", idx)
        elif "account" in hl or hl in {"coa", "account / coa"}:
            mapping.setdefault("account_code", idx)
        elif hl in {"invoice date", "date"}:
            mapping.setdefault("date", idx)
        elif hl == "description":
            mapping.setdefault("description", idx)
        elif hl in {
            "contact", "vendor", "customer", "vendor name", "customer name",
        }:
            mapping.setdefault("vendor", idx)
        elif ".pdf" in hl or "filename" in hl or "source" in hl:
            mapping.setdefault("filename", idx)
    return mapping


def _parse_delivery_data_table_rows(replies_resp: Any) -> list[dict]:
    """Parse ledger preview rows from delivery-card ``data_table`` blocks."""
    try:
        messages = (replies_resp or {}).get("messages") or []
    except Exception:  # noqa: BLE001
        return []
    if not messages:
        return []
    parent = messages[0]
    preview_rows: list[dict] = []
    for block in parent.get("blocks") or []:
        if not isinstance(block, dict) or block.get("type") != "data_table":
            continue
        table_rows = block.get("rows") or []
        if len(table_rows) < 2:
            continue
        headers = [_data_table_cell_text(c) for c in table_rows[0]]
        col_map = _map_delivery_table_headers(headers)
        if not col_map:
            continue
        for data_row in table_rows[1:]:
            cells = [_data_table_cell_text(c) for c in data_row]
            if not any(cells):
                continue
            record: dict[str, str] = {}
            for key, col_idx in col_map.items():
                if col_idx < len(cells) and cells[col_idx]:
                    record[key] = cells[col_idx]
            if record:
                preview_rows.append(record)
    return preview_rows


def _prefetch_thread_ledger_matches(
    ledger_rows: list[dict],
    *,
    invoice_ids: list[str],
    filenames: list[str],
    preview_rows: list[dict] | None = None,
    limit: int = 10,
) -> list[dict]:
    """Pre-resolve ledger row matches for thread-scoped invoice questions."""
    import re

    from accounting_agents.assistant import _normalize_row_for_tools
    from accounting_agents.assistant_tools._helpers import row_search_text

    needles: list[str] = []
    for inv in invoice_ids or []:
        token = str(inv or "").strip().lower()
        if token:
            needles.append(token)
    for fn in filenames or []:
        for inv in re.findall(r"\b\d{2}-D\d+\b", str(fn), re.IGNORECASE):
            needles.append(inv.lower())
    for row in preview_rows or []:
        inv = str(row.get("invoice_id") or "").strip().lower()
        if inv:
            needles.append(inv)
    needles = list(dict.fromkeys(n for n in needles if n))
    if not needles or not ledger_rows:
        return []

    matches: list[dict] = []
    for idx, raw in enumerate(ledger_rows):
        row = _normalize_row_for_tools(raw if isinstance(raw, dict) else {})
        text = row_search_text(row)
        if not any(n in text for n in needles):
            continue
        matches.append({
            "row_index": idx,
            "sheet": row.get("_sheet"),
            "account_code": (
                row.get("Account Code / COA")
                or row.get("*AccountCode")
                or row.get("category")
            ),
            "invoice_id": (
                row.get("*InvoiceNumber")
                or row.get("Invoice Number")
                or row.get("Reference")
            ),
            "description": row.get("Description") or row.get("*Description"),
            "vendor": row.get("Vendor") or row.get("*ContactName"),
            "date": row.get("Date") or row.get("*InvoiceDate"),
        })
        if len(matches) >= limit:
            break
    return matches


def _resolve_thread_delivery_context(
    *,
    raw_thread_ts: Optional[str],
    channel_id: Optional[str],
    processing_log: list[dict],
    slack_client: Any = None,
) -> dict:
    """Return thread-scoped delivery metadata for ``state_delta``.

    Phase 3 (thread context): when a user replies in the thread under a delivery
    card (ADR-0007 job summary), the chat lane should treat the question as about
    that delivery's files. We resolve that by:

    1. Filtering the client's processing_log for entries whose
       ``delivery_message_ts`` matches ``raw_thread_ts`` (Phase 2 wrote it).
    2. Falling back to ``conversations.replies`` parsing of the parent delivery
       message blocks when the Firestore filter is empty (older deliveries
       written before Phase 2 — pre-existing log entries lack the field).
    3. Returning an empty dict for top-level (non-thread) messages so the
       chat lane keeps its existing channel-wide behaviour.

    The returned dict keys (all optional) are meant for direct injection into
    ``state_delta`` so the assistant's instruction preamble and tools can
    read them. Keys follow the ``thread_delivery_*`` convention so they are
    easy to grep and so the chat instruction can name them with the
    ``{+key?+}`` ADK placeholder syntax.
    """
    import re

    if not raw_thread_ts:
        return {}

    replies_resp: Any = None
    if slack_client is not None and channel_id:
        try:
            replies_resp = slack_client.conversations_replies(
                channel=channel_id, ts=raw_thread_ts, limit=10,
            )
        except Exception:  # noqa: BLE001 — fallback is best-effort
            logger.debug(
                "conversations_replies failed for channel=%s ts=%s",
                channel_id, raw_thread_ts, exc_info=True,
            )

    preview_rows = _parse_delivery_data_table_rows(replies_resp)

    scoped: list[dict] = []
    for entry in processing_log or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("delivery_message_ts") or "") == str(raw_thread_ts):
            scoped.append(entry)

    # Fallback: older processing_log entries lack delivery_message_ts. Parse
    # the parent delivery message via conversations.replies to recover the
    # filenames (and FY, if present) so the chat lane still gets context.
    if not scoped and replies_resp is not None:
        parsed = _parse_thread_delivery_blocks(replies_resp)
        parent_fy = _delivery_fy_from_replies(replies_resp)
        scoped = _enrich_scoped_from_processing_log(
            parsed, processing_log, parent_fy=parent_fy,
        )

    if not scoped and not preview_rows:
        return {}

    filenames = [
        str(e.get("filename") or e.get("file_id") or "")
        for e in scoped
        if e.get("filename") or e.get("file_id")
    ]
    for prow in preview_rows:
        fn = str(prow.get("filename") or "").strip()
        if fn and fn not in filenames:
            filenames.append(fn)
    invoice_ids: list[str] = []
    for e in scoped:
        for inv in e.get("invoice_ids") or []:
            if inv:
                invoice_ids.append(str(inv))
        # Recover an invoice number from the filename when the log entry did
        # not have a separate invoice_ids list (the common case pre-Phase 2).
        if not e.get("invoice_ids"):
            fn = str(e.get("filename") or "")
            inv = str(e.get("invoice_id") or "")
            if inv and inv not in invoice_ids:
                invoice_ids.append(inv)
            elif fn:
                for inv in re.findall(r"\b\d{2}-D\d+\b", fn, re.IGNORECASE):
                    if inv not in invoice_ids:
                        invoice_ids.append(inv)
    for prow in preview_rows:
        inv = str(prow.get("invoice_id") or "").strip()
        if inv and inv not in invoice_ids:
            invoice_ids.append(inv)
    fy_counts: dict[str, int] = {}
    for e in scoped:
        fy = str(e.get("fy") or "").strip()
        if fy:
            fy_counts[fy] = fy_counts.get(fy, 0) + 1
    dominant_fy = (
        max(fy_counts.items(), key=lambda kv: kv[1])[0] if fy_counts else ""
    )
    if not dominant_fy and replies_resp is not None:
        dominant_fy = _delivery_fy_from_replies(replies_resp)

    return {
        "thread_delivery_message_ts": str(raw_thread_ts),
        "thread_delivery_filenames": filenames,
        "thread_delivery_invoice_ids": invoice_ids,
        "thread_delivery_fy": dominant_fy,
        "thread_delivery_preview_rows": preview_rows,
        "thread_scoped_processing_log": scoped,
    }


def _delivery_fy_from_replies(replies_resp: Any) -> str:
    """Extract FY label (e.g. ``2025``) from a delivery parent message."""
    import re

    try:
        messages = (replies_resp or {}).get("messages") or []
    except Exception:  # noqa: BLE001
        return ""
    if not messages:
        return ""
    parent = messages[0]
    chunks: list[str] = [str(parent.get("text") or "")]
    for block in parent.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"section", "rich_text"}:
            text_obj = block.get("text") or {}
            if isinstance(text_obj, dict) and text_obj.get("text"):
                chunks.append(str(text_obj["text"]))
    blob = "\n".join(chunks)
    m = re.search(r"FY\s*(\d{4})", blob, re.IGNORECASE)
    return m.group(1) if m else ""


def _enrich_scoped_from_processing_log(
    parsed: list[dict],
    processing_log: list[dict],
    *,
    parent_fy: str = "",
) -> list[dict]:
    """Join fallback parser output with real processing_log rows by filename."""
    if not parsed:
        return []
    enriched: list[dict] = []
    for item in parsed:
        fn = str(item.get("filename") or "").strip()
        inv = str(item.get("invoice_id") or "").strip()
        match: dict | None = None
        for entry in processing_log or []:
            if not isinstance(entry, dict):
                continue
            entry_fn = str(entry.get("filename") or "")
            if fn and entry_fn and (fn in entry_fn or entry_fn in fn):
                match = dict(entry)
                break
            if inv and inv in entry_fn:
                match = dict(entry)
                break
        if match:
            match.setdefault("filename", fn or match.get("filename") or "")
            if parent_fy and not match.get("fy"):
                match["fy"] = parent_fy
            enriched.append(match)
        else:
            row = dict(item)
            if parent_fy and not row.get("fy"):
                row["fy"] = parent_fy
            enriched.append(row)
    return enriched


def _parse_thread_delivery_blocks(replies_resp: Any) -> list[dict]:
    """Best-effort extraction of delivery metadata from a parent Slack message.

    Used as the fallback path for older deliveries whose processing_log entry
    does NOT carry ``delivery_message_ts``. We look for mrkdwn blocks,
    ``data_table`` cells, and invoice-id patterns (``25-D15``).
    """
    import re

    try:
        messages = (replies_resp or {}).get("messages") or []
    except Exception:  # noqa: BLE001
        return []
    if not messages:
        return []
    parent = messages[0]
    parent_fy = _delivery_fy_from_replies(replies_resp)
    filenames: list[str] = []
    invoice_ids: list[str] = []
    text_blobs: list[str] = [str(parent.get("text") or "")]
    for block in parent.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"section", "rich_text"}:
            text = (block.get("text") or {}).get("text") if block.get("text") else ""
            if not text and "elements" in block:
                parts = []
                for el in block["elements"] or []:
                    if isinstance(el, dict) and "text" in el:
                        parts.append(str(el["text"]))
                text = "".join(parts)
            if text:
                text_blobs.append(str(text))
            for line in (text or "").splitlines():
                cleaned = line.strip().strip("`*_~<>")
                for prefix in ("•", "-", "*", "·"):
                    if cleaned.startswith(prefix):
                        cleaned = cleaned[len(prefix):].strip()
                if cleaned.endswith(".pdf") or cleaned.endswith(".PDF"):
                    filenames.append(cleaned)
        elif block.get("type") == "data_table":
            for row in block.get("rows") or []:
                for cell in row or []:
                    txt = str((cell or {}).get("text") if isinstance(cell, dict) else "")
                    if not txt:
                        continue
                    if ".pdf" in txt.lower():
                        filenames.append(txt.strip())
                    for inv in re.findall(r"\b\d{2}-D\d+\b", txt, re.IGNORECASE):
                        if inv not in invoice_ids:
                            invoice_ids.append(inv)
    for blob in text_blobs:
        for inv in re.findall(r"\b\d{2}-D\d+\b", blob, re.IGNORECASE):
            if inv not in invoice_ids:
                invoice_ids.append(inv)
    entries: list[dict] = []
    seen: set[str] = set()
    for fn in filenames:
        key = fn.lower()
        if key in seen:
            continue
        seen.add(key)
        entries.append({
            "filename": fn, "file_id": "", "fy": parent_fy,
            "from_replies_fallback": True,
        })
    for inv in invoice_ids:
        if inv.lower() in seen:
            continue
        seen.add(inv.lower())
        entries.append({
            "filename": "", "invoice_id": inv, "file_id": "", "fy": parent_fy,
            "from_replies_fallback": True,
        })
    return entries


def _patch_processing_log_delivery_ts(
    client_store,
    *,
    client_id: str,
    channel_id: str,
    delivery_message_ts: str,
    file_ids: list[str],
    fy: str = "",
    per_file: list[dict] | None = None,
) -> None:
    """Backfill ``delivery_message_ts`` on batch processing_log entries (Phase 2)."""
    if not (client_store and client_id and delivery_message_ts and file_ids):
        return
    by_id: dict[str, dict] = {}
    for item in per_file or []:
        if isinstance(item, dict):
            fid = str(item.get("file_id") or "").strip()
            if fid:
                by_id[fid] = item
    for file_id in file_ids:
        fid = str(file_id or "").strip()
        if not fid:
            continue
        patch: dict = {
            "delivery_message_ts": delivery_message_ts,
            "channel_id": channel_id,
        }
        if fy:
            patch["fy"] = fy
        extra = by_id.get(fid) or {}
        if extra.get("row_count") is not None:
            patch["row_count"] = extra["row_count"]
        inv_ids = extra.get("invoice_ids")
        if inv_ids:
            patch["invoice_ids"] = list(inv_ids)
        try:
            client_store.append_processing_log(
                client_id=client_id, file_id=fid, entry=patch,
            )
        except Exception:  # noqa: BLE001 — best-effort
            logger.debug(
                "processing_log backfill failed client=%s file=%s",
                client_id, fid, exc_info=True,
            )


def _pick_chat_fy(
    *,
    best_fy: str | None,
    fy_summaries: list[dict],
    processing_log: list,
    question: str,
    fye_month: int | None,
    ledger_store: Any,
    client_id: str,
) -> str:
    """Choose which FY workbook to load for this chat turn (not hard-coded).

    Priority:
    1. User message matches a processing-log entry → that entry's FY (if workbook has data).
    2. ``best_fy_for_chat`` (most rows across pointers).
    3. Dominant FY in processing log (if that workbook has data).
    4. ``latest_fy`` / calendar FY fallback.
    """
    by_fy = {str(s.get("fy") or ""): s for s in fy_summaries if isinstance(s, dict)}

    def _row_count(fy_label: str) -> int:
        s = by_fy.get(str(fy_label), {})
        try:
            return int(s.get("row_count") or 0)
        except (TypeError, ValueError):
            return 0

    fy = best_fy or "unknown"

    hint_fy = _fy_hint_from_question(question, processing_log)
    if hint_fy and _row_count(hint_fy) > 0:
        return hint_fy

    if fy != "unknown" and _row_count(fy) > 0:
        return fy

    log_fy = _dominant_fy_from_processing_log(processing_log)
    if log_fy and _row_count(log_fy) > 0:
        return log_fy

    if fy != "unknown":
        return fy

    latest = ledger_store.latest_fy(client_id) if hasattr(ledger_store, "latest_fy") else None
    if latest:
        return str(latest)
    if fye_month:
        from datetime import date as _date
        from invoice_processing.export.fy import fy_for_date

        return str(fy_for_date(_date.today(), int(fye_month)))
    return "unknown"


async def _persist_direct_chat_turn(
    runner: Any,
    app_name: str,
    channel_id: str,
    session_id: str,
    *,
    question: str,
    answer: str,
    thread_focus: dict | None = None,
) -> None:
    """Append a synthetic user/model turn plus optional thread_focus to the session."""
    if not question and not answer and not thread_focus:
        return
    try:
        from google.adk.events.event import Event
        from google.adk.events.event_actions import EventActions

        session = await runner.session_service.get_session(
            app_name=app_name, user_id=channel_id, session_id=session_id
        )
        if session is None:
            return
        if question:
            await runner.session_service.append_event(
                session,
                Event(
                    author="user",
                    content=types.Content(
                        role="user",
                        parts=[types.Part(text=question)],
                    ),
                ),
            )
        if answer:
            await runner.session_service.append_event(
                session,
                Event(
                    author="assistant",
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=answer)],
                    ),
                ),
            )
        if thread_focus:
            await runner.session_service.append_event(
                session,
                Event(
                    author="assistant",
                    actions=EventActions(
                        state_delta={THREAD_FOCUS_KEY: thread_focus}
                    ),
                ),
            )
    except Exception:  # noqa: BLE001 — session history is best-effort
        logger.exception("failed to persist direct chat turn")


def _question_asks_account_code(question: str) -> bool:
    """True when the user is asking about a COA / account code in this thread."""
    import re

    q = (question or "").lower()
    if re.search(
        r"account\s*code|acount\s*code|why.*\bcoa\b|categoriz|why.*\d-\d|why.*\d{3}",
        q,
    ):
        return True
    if re.search(
        r"what.*(description|mean|name)|describe.*code|meaning of|"
        r"what does.*code|tell me more|explain.*code|that account|the account code",
        q,
    ):
        return True
    if re.search(r"\b\d{3}-[A-Z]\d{2}\b", question or "", re.I):
        return True
    return False


def _question_asks_coa_description(question: str) -> bool:
    """True when the user wants the COA name/meaning, not why it was chosen."""
    import re

    q = (question or "").lower()
    return bool(
        re.search(
            r"what.*(description|mean|name)|describe.*code|meaning of|"
            r"what does.*code|what is the description",
            q,
        )
    )


def _invoice_needles_from_question(question: str, thread_ctx: dict) -> list[str]:
    import re

    needles = [
        m.lower() for m in re.findall(r"\b\d{2}-D\d+\b", question or "", re.I)
    ]
    focus = thread_ctx.get(THREAD_FOCUS_KEY) or {}
    if isinstance(focus, dict):
        inv = str(focus.get("invoice_id") or "").lower()
        if inv and inv not in needles:
            needles.append(inv)
    for inv in thread_ctx.get("thread_delivery_invoice_ids") or []:
        token = str(inv).lower()
        if token and token not in needles:
            needles.append(token)
    return needles


def _build_thread_focus(
    *,
    invoice_id: str,
    account_code: str,
    vendor: str = "",
    line_description: str = "",
    row_index: int | None = None,
) -> dict:
    focus: dict = {
        "invoice_id": invoice_id,
        "account_code": account_code,
    }
    if vendor:
        focus["vendor"] = vendor
    if line_description:
        focus["line_description"] = line_description
    if row_index is not None:
        focus["row_index"] = row_index
    return focus


def _try_direct_thread_account_code_answer(
    question: str,
    *,
    state_delta: dict,
) -> tuple[str | None, dict | None]:
    """Compose a direct reply when thread context already has ledger rows.

    Returns ``(answer_text, thread_focus)`` or ``(None, None)`` when the direct
    path does not apply.
    """
    import json
    import re

    if not _question_asks_account_code(question):
        return None, None
    preview_rows = state_delta.get("thread_delivery_preview_rows") or []
    ledger_matches = state_delta.get("thread_delivery_ledger_matches") or []
    focus = state_delta.get(THREAD_FOCUS_KEY) or {}
    if not preview_rows and not ledger_matches and not focus:
        return None, None

    needles = _invoice_needles_from_question(question, state_delta)
    needle = needles[0] if needles else ""

    preview_hit: dict | None = None
    for row in preview_rows:
        inv = str(row.get("invoice_id") or "").lower()
        if not needle or inv == needle.lower():
            preview_hit = row
            break

    ledger_hit: dict | None = None
    for match in ledger_matches:
        inv = str(match.get("invoice_id") or "").lower()
        if not needle or inv == needle.lower() or needle.lower() in inv:
            ledger_hit = match
            break
    if not ledger_hit and ledger_matches:
        ledger_hit = ledger_matches[0]
    if not ledger_hit and isinstance(focus, dict) and focus.get("account_code"):
        ledger_hit = {
            "invoice_id": focus.get("invoice_id"),
            "account_code": focus.get("account_code"),
            "vendor": focus.get("vendor"),
            "description": focus.get("line_description"),
            "row_index": focus.get("row_index"),
        }

    acct = (ledger_hit or {}).get("account_code") or (preview_hit or {}).get(
        "account_code"
    ) or (focus.get("account_code") if isinstance(focus, dict) else "")
    if not acct:
        return None, None

    inv_label = (
        (ledger_hit or {}).get("invoice_id")
        or (preview_hit or {}).get("invoice_id")
        or (focus.get("invoice_id") if isinstance(focus, dict) else "")
        or (needle.upper() if needle else "")
        or "this invoice"
    )
    vendor = (ledger_hit or {}).get("vendor") or (preview_hit or {}).get("vendor") or ""
    desc = (
        (ledger_hit or {}).get("description")
        or (preview_hit or {}).get("description")
        or ""
    )
    row_index = (ledger_hit or {}).get("row_index")
    if row_index is None and isinstance(focus, dict):
        row_index = focus.get("row_index")

    thread_focus = _build_thread_focus(
        invoice_id=str(inv_label),
        account_code=str(acct),
        vendor=str(vendor or ""),
        line_description=str(desc or ""),
        row_index=int(row_index) if row_index is not None else None,
    )

    class _ToolCtx:
        def __init__(self, state: dict):
            self.state = state

    if _question_asks_coa_description(question):
        try:
            from accounting_agents.assistant import lookup_coa_account

            coa_raw = lookup_coa_account(_ToolCtx(state_delta), account_code=str(acct))
            coa = json.loads(coa_raw)
            if coa.get("status") == "found":
                name = coa.get("description") or ""
                acct_type = coa.get("account_type") or ""
                lines = [
                    f"Account code **{acct}** in your chart of accounts is "
                    f"**{name}**.",
                ]
                if acct_type:
                    lines.append(f"Account type: {acct_type}.")
                if inv_label and inv_label != "this invoice":
                    lines.append(
                        f"This is the code posted for invoice **{inv_label}** "
                        f"in this delivery."
                    )
                if desc:
                    lines.append(f"Line: {desc}.")
                return "\n\n".join(lines), thread_focus
        except Exception:  # noqa: BLE001
            logger.debug("direct COA description lookup failed", exc_info=True)

    acct_display = str(acct)
    reason_text = ""

    try:
        from accounting_agents.assistant import explain_categorization

        if ledger_hit and ledger_hit.get("row_index") is not None:
            expl = json.loads(
                explain_categorization(
                    _ToolCtx(state_delta),
                    row_index=str(ledger_hit["row_index"]),
                )
            )
        elif vendor and desc:
            expl = json.loads(
                explain_categorization(
                    _ToolCtx(state_delta),
                    vendor_name=vendor,
                    line_description=desc,
                )
            )
        else:
            expl = {}
        if expl.get("account_name"):
            acct_display = f"{acct} ({expl['account_name']})"
        if expl.get("reason"):
            reason_text = str(expl["reason"])
    except Exception:  # noqa: BLE001 — direct path still cites ledger row
        logger.debug("direct thread explain_categorization failed", exc_info=True)

    wrong_code = re.search(r"\b\d-\d{3,4}\b", question or "")
    lines = [
        f"Invoice **{inv_label}** in this batch is posted to account "
        f"**{acct_display}**.",
    ]
    if desc:
        lines.append(f"Line: {desc}.")
    if vendor:
        lines.append(f"Vendor: {vendor}.")
    if wrong_code and wrong_code.group(0) != str(acct):
        lines.append(
            f"(You asked about {wrong_code.group(0)} — the ledger row uses "
            f"**{acct}**, not that code.)"
        )
    if reason_text:
        lines.append(f"Categorization logic: {reason_text}")
    elif ledger_hit:
        lines.append("Source: FY ledger workbook row for this delivery.")
    else:
        lines.append("Source: delivery card table posted with this batch.")
    return "\n\n".join(lines), thread_focus


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
    doc_runner: Any = None,
    db: Any = None,
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
    thinking_ts = thread_ts or raw_thread_ts or message_ts
    ux = _chat_ux_enabled()
    answer_text = ""
    last_tool_text = ""
    if ux:
        _ack_chat_turn(slack_client, channel_id, message_ts)

    async with _SEM:
        await _ensure_session(runner, app_name, channel_id, session_id)

        # Resolve client profile so we have client_id + fye_month.
        profile_delta = _profile_state_delta(client_store, channel_id) if client_store else {}
        client_id = profile_delta.get("client_id") or channel_id
        fye_month = profile_delta.get("fye_month")

        processing_log: list[dict] = []
        if client_store and client_id:
            try:
                processing_log = await asyncio.to_thread(
                    client_store.list_processing_log, client_id, limit=20
                )
            except Exception:  # noqa: BLE001 — log read is non-fatal
                logger.exception(
                    "list_processing_log failed for client %s", client_id
                )
        if client_store and not processing_log and client_id:
            try:
                processing_log = await _lazy_backfill_processing_log(
                    client_store=client_store,
                    client_id=client_id,
                    channel_id=channel_id,
                    app_name=app_name,
                )
            except Exception:  # noqa: BLE001 — best-effort
                logger.exception(
                    "lazy backfill failed for client=%s channel=%s",
                    client_id, channel_id,
                )

        fy_summaries: list[dict] = []
        best_fy: str | None = None
        if hasattr(ledger_store, "best_fy_for_chat"):
            try:
                best_fy, fy_summaries = await asyncio.to_thread(
                    ledger_store.best_fy_for_chat, client_id, slack_client
                )
            except Exception:  # noqa: BLE001 — never block chat on FY selection
                logger.exception("best_fy_for_chat failed for client %s", client_id)

        fy = _pick_chat_fy(
            best_fy=best_fy,
            fy_summaries=fy_summaries,
            processing_log=processing_log,
            question=question,
            fye_month=fye_month,
            ledger_store=ledger_store,
            client_id=client_id,
        )

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
            "onboarding_required": not bool(profile_delta.get("software")),
            # Diagnostic counts for the assistant preamble + tools.
            "fy_loaded": fy,
            "ledger_row_count": len(ledger_rows),
            "fy_pointers": fy_summaries,
        }

        state_delta[PROCESSING_LOG_KEY] = processing_log
        state_delta["processing_log_count"] = len(processing_log)

        # Phase 3 (thread context): scope the chat turn to the delivery card
        # the user is replying under. Resolved BEFORE FY selection so the
        # thread-scoped FY can take priority in _pick_chat_fy.
        thread_ctx = _resolve_thread_delivery_context(
            raw_thread_ts=raw_thread_ts,
            channel_id=channel_id,
            processing_log=processing_log,
            slack_client=slack_client,
        )
        if thread_ctx:
            state_delta.update(thread_ctx)
            scoped_log = thread_ctx.get("thread_scoped_processing_log")
            if isinstance(scoped_log, list) and scoped_log:
                state_delta[PROCESSING_LOG_KEY] = scoped_log
                state_delta["processing_log_count"] = len(scoped_log)
            # Prefer the delivery batch FY over client-wide best_fy.
            thread_fy = thread_ctx.get("thread_delivery_fy") or ""
            if thread_fy:
                fy = thread_fy
                state_delta["fy_loaded"] = fy
                try:
                    ledger_rows = await asyncio.to_thread(
                        ledger_store.read_rows,
                        client_id=client_id,
                        fy=fy,
                        slack_client=slack_client,
                        channel_id=channel_id,
                    )
                    state_delta[LEDGER_DATA_KEY] = ledger_rows
                    state_delta["ledger_row_count"] = len(ledger_rows)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "thread-scoped read_rows failed channel=%s fy=%s",
                        channel_id, fy,
                    )
            preview_rows = thread_ctx.get("thread_delivery_preview_rows") or []
            if ledger_rows and (
                thread_ctx.get("thread_delivery_invoice_ids")
                or thread_ctx.get("thread_delivery_filenames")
                or preview_rows
            ):
                matches = _prefetch_thread_ledger_matches(
                    ledger_rows,
                    invoice_ids=thread_ctx.get("thread_delivery_invoice_ids") or [],
                    filenames=thread_ctx.get("thread_delivery_filenames") or [],
                    preview_rows=preview_rows,
                )
                if matches:
                    state_delta["thread_delivery_ledger_matches"] = matches

        # P1: inject pending HITL reviews for the channel so the chat agent
        # can answer "anything waiting on me?" without a Firestore round-trip.
        pending_reviews: list[dict] = []
        if db is not None:
            try:
                from accounting_agents.hitl import list_pending_interrupts
                pending_reviews = await asyncio.to_thread(
                    list_pending_interrupts, db, channel_id, limit=25
                )
            except Exception:  # noqa: BLE001 — diagnostic injection is best-effort
                logger.exception(
                    "list_pending_interrupts failed for channel %s", channel_id
                )
        state_delta["pending_reviews"] = pending_reviews
        state_delta["pending_review_count"] = len(pending_reviews)

        # P1: snapshot per-document session state for files referenced in the
        # processing log so the chat can introspect them read-only.
        file_ids: list[str] = []
        plog_for_snapshots = state_delta.get(PROCESSING_LOG_KEY) or processing_log
        for entry in plog_for_snapshots:
            if not isinstance(entry, dict):
                continue
            fid = entry.get("file_id")
            if fid:
                file_ids.append(str(fid))
        doc_sessions: dict = {}
        if runner and file_ids:
            try:
                doc_sessions = await _snapshot_doc_sessions(
                    runner, app_name, channel_id, file_ids
                )
            except Exception:  # noqa: BLE001 — best-effort
                logger.exception(
                    "_snapshot_doc_sessions failed for channel %s", channel_id
                )
        state_delta["document_sessions"] = doc_sessions

        # Pre-write the freshly-fetched ledger rows into the session state NOW,
        # before run_async, so the value is unconditionally overwritten on every
        # turn.  ADK's run_async applies state_delta via append_event (which
        # updates session.state in-place), but that only happens as part of the
        # user-message event inside the invocation.  Pre-writing here is a
        # belt-and-suspenders guard that ensures stale ledger_data from a prior
        # turn (e.g. the initial empty-ledger turn) can never mask a
        # freshly-posted delivery — regardless of runner internals.
        # (P0-2 fix, 2026-06-15)
        await _apply_state_delta(
            runner, app_name, channel_id, session_id,
            state_delta,
        )

        try:
            live_session = await runner.session_service.get_session(
                app_name=app_name, user_id=channel_id, session_id=session_id
            )
            if live_session and live_session.state.get(THREAD_FOCUS_KEY):
                state_delta[THREAD_FOCUS_KEY] = live_session.state[THREAD_FOCUS_KEY]
        except Exception:  # noqa: BLE001
            pass

        direct_answer, thread_focus = _try_direct_thread_account_code_answer(
            question, state_delta=state_delta,
        )
        if direct_answer:
            if thread_focus:
                state_delta[THREAD_FOCUS_KEY] = thread_focus
                await _apply_state_delta(
                    runner, app_name, channel_id, session_id,
                    {THREAD_FOCUS_KEY: thread_focus},
                )
            await _persist_direct_chat_turn(
                runner,
                app_name,
                channel_id,
                session_id,
                question=question,
                answer=direct_answer,
                thread_focus=thread_focus,
            )
            if ux:
                _clear_chat_thinking(slack_client, channel_id, thinking_ts)
                _finish_chat_ack(slack_client, channel_id, message_ts)
            _post_message(
                slack_client, channel_id, direct_answer, thread_ts=thread_ts,
            )
            return {
                "status": "answered",
                "text": direct_answer,
                "direct": True,
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

        if ux:
            _set_chat_thinking(slack_client, channel_id, thinking_ts)

        # Chat-lane confirm bridge (ADR-0009): the user's "yes" arrives as plain
        # Slack text, not a FunctionResponse. If the session has an unanswered
        # ``adk_request_confirmation`` and this message is clearly affirmative /
        # negative, synthesize the FunctionResponse so ADK re-runs the gated tool;
        # otherwise pass the raw text through (the model handles ambiguity).
        new_message = types.Content(
            role="user", parts=[types.Part(text=question)]
        )
        fc_id: Optional[str] = None
        try:
            pre_session = await runner.session_service.get_session(
                app_name=app_name, user_id=channel_id, session_id=session_id
            )
        except Exception:  # noqa: BLE001 — a session read failure must not block chat
            pre_session = None
        pending_confirm = find_pending_confirmation(pre_session) if pre_session else None
        if pending_confirm is not None:
            verdict = classify_confirmation_reply(question)
            if verdict is not None:
                fc_id, confirmation = pending_confirm
                new_message = _synthesize_confirmation_message(
                    fc_id, confirmation, confirmed=verdict
                )

        answer_text = ""
        last_tool_text = ""
        async for event in runner.run_async(
            user_id=channel_id,
            session_id=session_id,
            new_message=new_message,
            state_delta=state_delta,
            run_config=run_config,
        ):
            if ux:
                for tool_name in _function_call_names(event):
                    stage = _CHAT_TOOL_LOADING.get(tool_name)
                    if stage:
                        _set_chat_thinking(
                            slack_client, channel_id, thinking_ts, stage=stage
                        )
            text = extract_final_text(event)
            if text:
                answer_text = text
            tool_text = extract_tool_response_text(event)
            if tool_text:
                last_tool_text = tool_text

        # Post-run: apply any confirmed writes the tool queued in state, then
        # refresh ``ledger_data`` so the rest of the chat reflects the change.
        try:
            post_session = await runner.session_service.get_session(
                app_name=app_name, user_id=channel_id, session_id=session_id
            )
            post_state = dict(post_session.state) if post_session else {}
        except Exception:  # noqa: BLE001
            post_state = {}

        if post_state.get(PENDING_WRITE_KEY):
            committed = await _execute_pending_writes(
                state=post_state,
                ledger_store=ledger_store,
                slack_client=slack_client,
                channel_id=channel_id,
                client_id=client_id,
                fy=fy,
                session_id=session_id,
                fc_id=fc_id,
                thread_ts=thread_ts,
            )
            # Persist the cleared pending list + idempotency marker, and refresh
            # the loaded ledger rows so subsequent turns see the new state.
            write_back = {
                PENDING_WRITE_KEY: [],
                "committed_confirmations": post_state.get("committed_confirmations", []),
            }
            if committed:
                try:
                    refreshed = await asyncio.to_thread(
                        ledger_store.read_rows,
                        client_id=client_id,
                        fy=fy,
                        slack_client=slack_client,
                        channel_id=channel_id,
                    )
                    write_back[LEDGER_DATA_KEY] = refreshed
                except Exception:  # noqa: BLE001
                    logger.exception("ledger refresh after chat write failed")
            await _apply_state_delta(
                runner, app_name, channel_id, session_id, write_back
            )

        # Drain pending_learn_mapping: persist each vendor rule to entity_memory
        # via client_store.add_correction so the next document run picks it up.
        pending_learn = post_state.get(PENDING_LEARN_KEY)
        if isinstance(pending_learn, list) and pending_learn:
            _effective_client_store = client_store or _DEFAULT_CLIENT_STORE
            for entry in pending_learn:
                if not isinstance(entry, dict):
                    continue
                try:
                    _effective_client_store.add_correction(
                        client_id=client_id,
                        vendor=entry["vendor"],
                        account_code=entry.get("account_code"),
                        tax_code=entry.get("tax_code"),
                    )
                    logger.info(
                        "CHAT_LEARN_AUDIT session=%s channel=%s client=%s "
                        "vendor=%r account_code=%r tax_code=%r",
                        session_id, channel_id, client_id,
                        entry["vendor"], entry.get("account_code"), entry.get("tax_code"),
                    )
                except Exception:  # noqa: BLE001 — a failed learn must not crash the lane
                    logger.exception(
                        "learn_mapping drain failed: client=%s vendor=%r",
                        client_id, entry.get("vendor"),
                    )
            await _apply_state_delta(
                runner, app_name, channel_id, session_id, {PENDING_LEARN_KEY: []}
            )

    # Drain pending_reextract OUTSIDE the chat semaphore (ADR-0010): each spec
    # re-runs the FULL document pipeline via process_file_event, which acquires
    # the same module-level ``_SEM`` itself — running it inside this block would
    # deadlock at concurrency 1. doc_runner is the document graph runner (the chat
    # runner cannot drive the doc pipeline); the _message handler injects it.
    pending_reextract = post_state.get(PENDING_REEXTRACT_KEY)
    if isinstance(pending_reextract, list) and pending_reextract:
        if doc_runner is not None:
            await _execute_pending_reextract(
                pending_reextract,
                doc_runner=doc_runner,
                ledger_store=ledger_store,
                db=db,
                slack_client=slack_client,
                channel_id=channel_id,
                app_name=app_name,
                client_store=client_store,
                thread_ts=thread_ts,
            )
        else:
            logger.warning(
                "pending_reextract present but no doc_runner injected (channel=%s) "
                "— cannot re-run the document pipeline.",
                channel_id,
            )
        # Clear the key regardless so a re-extract is never retried unbounded.
        await _apply_state_delta(
            runner, app_name, channel_id, session_id, {PENDING_REEXTRACT_KEY: []}
        )

    if not answer_text:
        # Safety net: model went silent after a tool call. Show the user the
        # tool's raw result — better than the opaque "rephrase" canned message.
        answer_text = last_tool_text or (
            "I couldn't find an answer. Please try rephrasing your question."
        )

    if ux:
        _finish_chat_ack(slack_client, channel_id, message_ts)

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


def build_runner(*, session_service=None, artifact_service=None, direct_document: bool = True):
    """Construct the ADK ``Runner`` bound to ``document_app`` + Firestore sessions.

    File uploads always use ``document_app`` (root_agent=document_workflow).
    The LLM RouteDecision coordinator has been retired (ADR-0021) — routing is
    deterministic and lives in the Slack layer.

    Imports are deferred so importing this module never touches the network.
    """
    from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
    from google.adk.runners import Runner

    from accounting_agents.agent import document_app
    from accounting_agents.sessions import FirestoreSessionService

    logger.info("build_runner: using document_app (deterministic document entry, ADR-0021)")
    return Runner(
        app=document_app,
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

    The channel is already named after the client (e.g. ``sample-channel-client-pte-ltd``),
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
    installation_store=None,
    state_store=None,
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

    from app.commands import ledgr_slash_command_name
    from app.slack_app import (
        handle_ledgr_command,
        handle_member_joined,
        handle_onboarding_submit,
        handle_setup_open,
    )

    if store is None:
        from invoice_processing.export.client_context import FirestoreClientStore
        store = FirestoreClientStore()

    if chat_runner is None:
        chat_runner = build_chat_runner()

    app_name = runner.app_name
    token = bot_token or os.environ.get("SLACK_BOT_TOKEN")

    # Multi-workspace OAuth (distribution) vs single-token (socket/dev) mode.
    # When the full OAuth config is present (SLACK_CLIENT_ID/SECRET,
    # SLACK_SIGNING_SECRET, SLACK_BASE_URL — see app.config.missing_slack_oauth),
    # build the app in OAuth mode so other firms can self-install via the public
    # "Add to Slack" link. Otherwise fall back to the bot-token mode the socket
    # path + tests rely on (the socket entrypoint strips the OAuth env vars first,
    # so this branch is never taken there). The Firestore-backed stores' ``_db()``
    # is lazy, so constructing the defaults touches no network.
    import app.config as _app_config

    if not _app_config.missing_slack_oauth():
        from app.installation_store import (
            FirestoreInstallationStore,
            FirestoreOAuthStateStore,
        )
        from app.slack_app import BOT_SCOPES
        from slack_bolt.oauth.async_oauth_settings import AsyncOAuthSettings

        settings = _app_config.get_settings()
        if installation_store is None:
            installation_store = FirestoreInstallationStore()
        if state_store is None:
            state_store = FirestoreOAuthStateStore()
        # NOTE: bolt-python's AsyncOAuthSettings has NO ``state_secret`` kwarg
        # (that is a bolt-js concept). The state CSRF value is issued + verified
        # by the ``state_store`` together with a signed browser cookie; no
        # separate secret is accepted here. (Confirmed against the installed
        # slack_bolt/oauth/async_oauth_settings.py signature.)
        async_app = AsyncApp(
            signing_secret=settings.slack_signing_secret,
            oauth_settings=AsyncOAuthSettings(
                client_id=settings.slack_client_id,
                client_secret=settings.slack_client_secret,
                scopes=BOT_SCOPES,
                installation_store=installation_store,
                state_store=state_store,
                install_path="/slack/install",
                redirect_uri_path="/slack/oauth_redirect",
            ),
        )
    else:
        async_app = AsyncApp(token=token)

    # Bolt hands async handlers an AsyncWebClient, but ALL our downstream Slack Web
    # API calls (files_info, chat_postMessage, reactions, files_upload_v2,
    # files_delete, conversations_info) + the parked sync handlers are written
    # synchronously. Use one sync WebClient (same bot token) for every Web API call;
    # the async `client` is only used for Bolt's `ack()`. This is why uploads
    # silently did nothing before — sync calls on the async client returned
    # un-awaited coroutines.
    from slack_sdk import WebClient as _SyncWebClient

    def _sync_client_for(context=None, client=None):
        """Per-workspace sync WebClient. OAuth mode: context['bot_token'] is the
        installing workspace's token (Bolt authorize via installation_store);
        token mode: the single bot token. Falls back to the injected client's
        token, then the build-time token.

        This is the multi-workspace correctness fix: every listener rebinds its
        local ``sync_client`` from THIS, so an event from firm A never posts with
        firm B's token (the previous single global client did).
        """
        # Only accept a real string token. Bolt sets context['bot_token'] (and the
        # injected client's .token) to the per-workspace token; guarding on str
        # avoids building a live WebClient from a non-string (e.g. a test
        # MagicMock's auto-attribute), which would otherwise make real network
        # calls. Falls through to the build-time ``token``.
        tok = None
        if context is not None:
            ctx_tok = context.get("bot_token")
            if isinstance(ctx_tok, str):
                tok = ctx_tok
        if not tok and client is not None:
            client_tok = getattr(client, "token", None)
            if isinstance(client_tok, str):
                tok = client_tok
        return _SyncWebClient(token=tok or token)

    # Defensive default for any non-handler reference; every listener below
    # shadows this per-event via ``_sync_client_for(context, client)``.
    sync_client = _SyncWebClient(token=token)  # noqa: F841 - defensive default; listeners shadow per-event

    @async_app.event("file_shared")
    async def _file_shared(event, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        eid = body.get("event_id") or f"{event.get('type')}:{event.get('event_ts') or event.get('ts')}"
        if _seen.seen_before(eid):
            logger.debug("dedup: dropping duplicate file_shared event %s", eid)
            return
        file_id = event.get("file_id") or event.get("file", {}).get("id")
        channel_id = event.get("channel_id") or event.get("channel")
        if not file_id or not channel_id:
            return
        # COA-routing path B (ADR-0006): spreadsheets dropped on a not-yet-onboarded
        # / pending_coa / active channel are OFFERED as a COA (parse-back confirm
        # card) so the user explicitly approves ingestion. The previous
        # auto-routing skipped this confirmation step (the design intent in
        # ADR-0006) and accidentally treated any pending_coa xlsx as a COA.
        _resolved = store.get_by_channel(channel_id)
        file_payload = event.get("file") or {"id": file_id}

        from app.slack_app import _is_spreadsheet
        if isinstance(file_payload, dict) and _is_spreadsheet(file_payload):
            if _seen.seen_before(f"file:{file_id}"):
                logger.debug("dedup: file %s already being processed", file_id)
                return
            await _offer_coa_confirmation(
                sync_client=sync_client,
                channel_id=channel_id,
                file_id=file_id,
                file_payload=file_payload,
                channel_state=_channel_state_label(_resolved),
            )
            return

        # Document uploads: the message/file_share handler is the sole owner of
        # document processing. file_shared firing first used to stampede Gemini
        # with N parallel process_file_event calls (no thread_ts, no
        # defer_slack_delivery) before the batch coordinator could post its single
        # job summary. Now we drop the event silently — the message handler
        # carries thread_ts + defer flags and the dedup via `file:{id}` in
        # message handler ensures no double-processing if a future race appears.
        logger.debug(
            "file_shared: deferring document %s to message/file_share handler for channel %s",
            file_id, channel_id,
        )

    async def _run_action(ack, body, client, decision, sync_client):
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
    async def _approve(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        await _run_action(ack, body, client, "approve", sync_client)

    @async_app.action("edit")
    async def _edit(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
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
    async def _edit_submit(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
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
    async def _reject(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        await _run_action(ack, body, client, "reject", sync_client)

    # --- mid-flow extract-review HITL (review_extraction_node) ---

    async def _run_review_action(ack, body, action_str, sync_client, hint=None):
        await ack()
        action = (body.get("actions") or [{}])[0]
        op_id = action.get("value")
        if not op_id:
            return
        await handle_review_action(
            runner=runner,
            ledger_store=ledger_store,
            db=db,
            slack_client=sync_client,
            op_id=op_id,
            action=action_str,
            app_name=app_name,
            hint=hint,
        )

    @async_app.action("review_confirm")
    async def _review_confirm(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        await _run_review_action(ack, body, "confirm_as_is", sync_client)

    @async_app.action("review_reject")
    async def _review_reject(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        await _run_review_action(ack, body, "reject", sync_client)

    @async_app.action("review_reextract")
    async def _review_reextract(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        # Open a hint-input modal so the human can describe what the extractor
        # missed.  Mirrors the Edit button: ack() immediately, then views_open
        # with the trigger_id (Slack invalidates it after a few seconds).
        await ack()
        op_id = (body.get("actions") or [{}])[0].get("value")
        if not op_id:
            return
        sync_client.views_open(
            trigger_id=body["trigger_id"],
            view=review_hint_modal(op_id),
        )

    @async_app.view("ledgr_review_hint")
    async def _review_hint_submit(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        await ack()
        view = body["view"]
        op_id = view.get("private_metadata") or ""
        hint_text = (
            view.get("state", {})
            .get("values", {})
            .get("hint_block", {})
            .get("hint_input", {})
            .get("value")
            or ""
        )
        await handle_review_action(
            runner=runner,
            ledger_store=ledger_store,
            db=db,
            slack_client=sync_client,
            op_id=op_id,
            action="reextract_as",
            app_name=app_name,
            hint=hint_text or None,
        )

    # --- Step 8: proactive post-delivery re-extract offer ---

    @async_app.action("proactive_redo")
    async def _proactive_redo(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        # The doc is already filed (no paused interrupt). Open a hint modal so the
        # human can describe what the extractor missed; submit runs the re-extract.
        await ack()
        file_id = (body.get("actions") or [{}])[0].get("value")
        if not file_id:
            return
        sync_client.views_open(
            trigger_id=body["trigger_id"],
            view=proactive_redo_modal(file_id),
        )

    # --- COA confirmation card actions (ADR-0006 path A) ---

    async def _handle_coa_confirm(ack, body, client, sync_client, *, message_ts: str) -> None:
        await ack()
        action = (body.get("actions") or [{}])[0]
        file_id = (action.get("value") or "").strip()
        channel_id = (body.get("channel") or {}).get("id") or ""
        if not file_id or not channel_id:
            return
        # Use the existing ingest pipeline; the confirm click IS the activation
        # gate required by ADR-0006.
        import shutil as _shutil
        import tempfile as _tempfile

        from app.slack_app import run_coa_ingest as _run_coa_ingest
        from app.slack_app import slack_download_file as _dl

        def _say(**kwargs):
            sync_client.chat_postMessage(channel=channel_id, **kwargs)

        task_dir = _tempfile.mkdtemp(prefix="ledgr_coa_confirm_")
        try:
            local_path = await asyncio.to_thread(_dl, sync_client, file_id, task_dir)
            await asyncio.to_thread(
                _run_coa_ingest,
                channel_id=channel_id,
                file_path=local_path,
                store=store,
                say_fn=_say,
            )
        except Exception:  # noqa: BLE001 - cosmetic; reply so the user knows
            logger.exception("ledgr_coa_confirm: ingest failed for %s", file_id)
            _say(
                text=(
                    ":warning: I couldn't use that file as a COA. "
                    "Re-upload and try again."
                ),
            )
        finally:
            _shutil.rmtree(task_dir, ignore_errors=True)
        # Acknowledge the card so the user sees their click was received.
        try:
            sync_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text="COA confirmed.",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": ":white_check_mark: COA confirmed.",
                        },
                    }
                ],
            )
        except Exception:  # noqa: BLE001 - cosmetic
            logger.debug("ledgr_coa_confirm: could not update confirm card")

    @async_app.action("ledgr_coa_confirm")
    async def _ledgr_coa_confirm(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        message_ts = (body.get("message") or {}).get("ts") or ""
        await _handle_coa_confirm(ack, body, client, sync_client, message_ts=message_ts)

    @async_app.action("ledgr_coa_as_document")
    async def _ledgr_coa_as_document(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        # User explicitly chose "process as document" — fall through to the
        # document pipeline. Re-download the file and forward to process_file_event.
        await ack()
        action = (body.get("actions") or [{}])[0]
        file_id = (action.get("value") or "").strip()
        channel_id = (body.get("channel") or {}).get("id") or ""
        message_ts = (body.get("message") or {}).get("ts") or ""
        if not file_id or not channel_id:
            return
        # Soft-gate on client profile: same check as the document path. If the
        # client isn't onboarded yet, nudge them to set up first.
        client_store_local = _DEFAULT_CLIENT_STORE
        profile_delta = _profile_state_delta(client_store_local, channel_id)
        if not profile_delta or not profile_delta.get("software"):
            sync_client.chat_postMessage(
                channel=channel_id,
                text=(
                    "I don't have this client set up yet — run */ledgr settings* to "
                    "choose the accounting software and financial year, then re-drop the document."
                ),
            )
            return
        # The original file is already in Slack storage; re-route through the
        # standard document path. We use a fresh file_id look-up so the existing
        # download/validation pipeline does the right thing.
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
                thread_ts=None,
                source_filename=_resolve_file_name(sync_client, file_id, None),
                hint="",
                replace=False,
                defer_slack_delivery=False,
                batch_mode=False,
                defer_ledger_persist=False,
                status_callback=None,
            )
            logger.info(
                "ledgr_coa_as_document: processed %s as document status=%s",
                file_id, (result or {}).get("status"),
            )
        except Exception:  # noqa: BLE001 - cosmetic
            logger.exception(
                "ledgr_coa_as_document: process_file_event failed for %s", file_id
            )
            sync_client.chat_postMessage(
                channel=channel_id,
                text=(
                    ":warning: I couldn't process that file as a document. "
                    "Re-upload and try again."
                ),
            )
            return
        try:
            sync_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text="Processing as document.",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": ":page_facing_up: Processing as document.",
                        },
                    }
                ],
            )
        except Exception:  # noqa: BLE001
            logger.debug("ledgr_coa_as_document: could not update card")

    @async_app.action("ledgr_coa_cancel")
    async def _ledgr_coa_cancel(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        await ack()
        message_ts = (body.get("message") or {}).get("ts") or ""
        channel_id = (body.get("channel") or {}).get("id") or ""
        if not channel_id or not message_ts:
            return
        try:
            sync_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text="COA upload cancelled.",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": ":x: COA upload cancelled.",
                        },
                    }
                ],
            )
        except Exception:  # noqa: BLE001
            logger.debug("ledgr_coa_cancel: could not update card")

    @async_app.view("ledgr_proactive_redo")
    async def _proactive_redo_submit(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        # The button-click + modal-submit IS the confirmation — run the re-extract
        # directly (NOT a paused-interrupt resume; the document is already filed).
        await ack()
        view = body["view"]
        file_id = (view.get("private_metadata") or "").strip()
        hint_text = (
            view.get("state", {})
            .get("values", {})
            .get("hint_block", {})
            .get("hint_input", {})
            .get("value")
            or ""
        ).strip()
        if not file_id or not hint_text:
            return
        # The view_submission body has no source channel; recover it (and the
        # upload ts to thread under) from the file's own share record.
        channel_id, upload_ts = _resolve_file_channel(sync_client, file_id)
        if not channel_id:
            logger.warning(
                "proactive_redo: could not resolve channel for file %s — skipping.",
                file_id,
            )
            return
        # Reuse the Step-7 re_extract drain (full pipeline + replace=True + the
        # CHAT_REEXTRACT_AUDIT log + the per-(file_id,hint) idempotency marker).
        await _execute_pending_reextract(
            [{"op": "reextract", "file_id": file_id, "hints": hint_text}],
            doc_runner=runner,
            ledger_store=ledger_store,
            db=db,
            slack_client=sync_client,
            channel_id=channel_id,
            app_name=app_name,
            client_store=store,
            thread_ts=upload_ts,
        )

    # --- per-doc card inline action handlers ---

    @async_app.action("ledgr_per_doc_reextract")
    async def _per_doc_reextract(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        # Same flow as proactive_redo: open the hint modal; the submit handler
        # (ledgr_proactive_redo) will drain it via _execute_pending_reextract.
        await ack()
        file_id = (body.get("actions") or [{}])[0].get("value")
        if not file_id:
            return
        sync_client.views_open(
            trigger_id=body["trigger_id"],
            view=proactive_redo_modal(file_id),
        )

    @async_app.action("ledgr_per_doc_edit")
    async def _per_doc_edit(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        # Editing an already-filed doc is not yet supported via the per-doc card.
        # Post an ephemeral explaining the limitation; full filed-doc edit is a
        # follow-up commit.
        await ack()
        channel_id_edit = (body.get("channel") or {}).get("id") or ""
        user_id = (body.get("user") or {}).get("id") or ""
        if channel_id_edit and user_id:
            try:
                sync_client.chat_postEphemeral(
                    channel=channel_id_edit,
                    user=user_id,
                    text=(
                        "Editing already-filed docs isn't supported yet — "
                        "try *Re-extract* instead to re-read the document with a hint."
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.debug("per_doc_edit: could not post ephemeral")

    @async_app.action("ledgr_per_doc_view_row")
    async def _per_doc_view_row(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        await ack()
        channel_id_vr = (body.get("channel") or {}).get("id") or ""
        user_id = (body.get("user") or {}).get("id") or ""
        if channel_id_vr and user_id:
            try:
                sync_client.chat_postEphemeral(
                    channel=channel_id_vr,
                    user=user_id,
                    text="(coming soon) View row will jump to the workbook line.",
                )
            except Exception:  # noqa: BLE001
                logger.debug("per_doc_view_row: could not post ephemeral")

    # --- Commit 5: feedback buttons under delivered per-doc cards ---

    def _handle_feedback_action(action_value: str, channel_id_fb: str, user_id_fb: str):
        """Shared logic for both native (ledgr_doc_feedback) and fallback handlers.

        Parses ``pos|<doc_ref>`` or ``neg|<doc_ref>`` from action_value and:
        - pos: queues a PENDING_LEARN_MAPPING entry so the next chat drain persists
          the vendor → account → tax mapping via client_store.add_correction.
        - neg: opens the proactive_redo_modal pre-populated with hint="user flagged via 👎".
        Then posts an ephemeral acknowledgement.
        """
        # Not used for modal open (no trigger_id available here); neg uses redo_modal via
        # a separate path driven by the body's trigger_id in the outer handlers below.
        pass  # implemented inline in each handler; this function documents the contract.

    @async_app.action("ledgr_doc_feedback")
    async def _doc_feedback(ack, body, client, context=None):
        """Native context_actions feedback_buttons handler.

        Button value format: ``pos|<doc_ref>`` or ``neg|<doc_ref>``
        where doc_ref = ``file_id|vendor|account_code|tax_code`` (%-encoded fields).
        """
        sync_client = _sync_client_for(context, client)
        await ack()
        action = (body.get("actions") or [{}])[0]
        raw_value = action.get("value") or ""
        channel_id_fb = (body.get("channel") or {}).get("id") or ""
        user_id_fb = (body.get("user") or {}).get("id") or ""

        # Split into polarity + doc_ref
        if "|" not in raw_value:
            return
        polarity, doc_ref = raw_value.split("|", 1)

        # Parse doc_ref: file_id|vendor|account_code|tax_code
        parts = doc_ref.split("|", 3)
        file_id_fb = urllib.parse.unquote(parts[0]) if len(parts) > 0 else ""
        vendor_fb = urllib.parse.unquote(parts[1]) if len(parts) > 1 else ""
        account_code_fb = urllib.parse.unquote(parts[2]) if len(parts) > 2 else ""
        tax_code_fb = urllib.parse.unquote(parts[3]) if len(parts) > 3 else ""

        if polarity == "pos":
            # 👍 — persist the vendor → account → tax mapping (same drain path as chat learn_mapping)
            if vendor_fb and vendor_fb != "-":
                try:
                    _effective_store = store or _DEFAULT_CLIENT_STORE
                    client_id_fb = _effective_store.get_client_id(channel_id=channel_id_fb) if hasattr(_effective_store, "get_client_id") else channel_id_fb
                    _effective_store.add_correction(
                        client_id=client_id_fb,
                        vendor=vendor_fb,
                        account_code=account_code_fb if account_code_fb and account_code_fb != "-" else None,
                        tax_code=tax_code_fb if tax_code_fb and tax_code_fb != "-" else None,
                    )
                    logger.info(
                        "FEEDBACK_LEARN_AUDIT channel=%s vendor=%r account_code=%r tax_code=%r",
                        channel_id_fb, vendor_fb, account_code_fb, tax_code_fb,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("feedback 👍: add_correction failed channel=%s vendor=%r", channel_id_fb, vendor_fb)
        elif polarity == "neg":
            # 👎 — open the proactive-redo modal pre-populated with prefill hint
            file_id_clean = file_id_fb if file_id_fb and file_id_fb != "-" else ""
            if file_id_clean:
                try:
                    modal = proactive_redo_modal(file_id_clean)
                    sync_client.views_open(
                        trigger_id=body.get("trigger_id", ""),
                        view=modal,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("feedback 👎: views_open failed channel=%s file=%s", channel_id_fb, file_id_clean)

        # Ephemeral acknowledgement in both cases.
        if channel_id_fb and user_id_fb:
            try:
                sync_client.chat_postEphemeral(
                    channel=channel_id_fb,
                    user=user_id_fb,
                    text="Thanks — recorded your feedback.",
                )
            except Exception:  # noqa: BLE001
                logger.debug("doc_feedback: could not post ephemeral")

    @async_app.action("ledgr_doc_feedback_pos")
    async def _doc_feedback_pos(ack, body, client, context=None):
        """Fallback 👍 handler (actions block path)."""
        sync_client = _sync_client_for(context, client)
        await ack()
        action = (body.get("actions") or [{}])[0]
        raw_value = action.get("value") or ""
        channel_id_fb = (body.get("channel") or {}).get("id") or ""
        user_id_fb = (body.get("user") or {}).get("id") or ""

        # raw_value is ``pos|doc_ref`` — strip polarity prefix
        doc_ref = raw_value[len("pos|"):] if raw_value.startswith("pos|") else raw_value
        parts = doc_ref.split("|", 3)
        vendor_fb = urllib.parse.unquote(parts[1]) if len(parts) > 1 else ""
        account_code_fb = urllib.parse.unquote(parts[2]) if len(parts) > 2 else ""
        tax_code_fb = urllib.parse.unquote(parts[3]) if len(parts) > 3 else ""

        if vendor_fb and vendor_fb != "-":
            try:
                _effective_store = store or _DEFAULT_CLIENT_STORE
                client_id_fb = _effective_store.get_client_id(channel_id=channel_id_fb) if hasattr(_effective_store, "get_client_id") else channel_id_fb
                _effective_store.add_correction(
                    client_id=client_id_fb,
                    vendor=vendor_fb,
                    account_code=account_code_fb if account_code_fb and account_code_fb != "-" else None,
                    tax_code=tax_code_fb if tax_code_fb and tax_code_fb != "-" else None,
                )
                logger.info(
                    "FEEDBACK_LEARN_AUDIT channel=%s vendor=%r account_code=%r tax_code=%r (fallback)",
                    channel_id_fb, vendor_fb, account_code_fb, tax_code_fb,
                )
            except Exception:  # noqa: BLE001
                logger.exception("feedback_pos: add_correction failed channel=%s vendor=%r", channel_id_fb, vendor_fb)

        if channel_id_fb and user_id_fb:
            try:
                sync_client.chat_postEphemeral(
                    channel=channel_id_fb,
                    user=user_id_fb,
                    text="Thanks — recorded your feedback.",
                )
            except Exception:  # noqa: BLE001
                logger.debug("doc_feedback_pos: could not post ephemeral")

    @async_app.action("ledgr_doc_feedback_neg")
    async def _doc_feedback_neg(ack, body, client, context=None):
        """Fallback 👎 handler (actions block path)."""
        sync_client = _sync_client_for(context, client)
        await ack()
        action = (body.get("actions") or [{}])[0]
        raw_value = action.get("value") or ""
        channel_id_fb = (body.get("channel") or {}).get("id") or ""
        user_id_fb = (body.get("user") or {}).get("id") or ""

        # raw_value is ``neg|doc_ref`` — strip polarity prefix
        doc_ref = raw_value[len("neg|"):] if raw_value.startswith("neg|") else raw_value
        parts = doc_ref.split("|", 3)
        file_id_fb = urllib.parse.unquote(parts[0]) if len(parts) > 0 else ""

        file_id_clean = file_id_fb if file_id_fb and file_id_fb != "-" else ""
        if file_id_clean:
            try:
                modal = proactive_redo_modal(file_id_clean)
                sync_client.views_open(
                    trigger_id=body.get("trigger_id", ""),
                    view=modal,
                )
            except Exception:  # noqa: BLE001
                logger.exception("feedback_neg: views_open failed channel=%s file=%s", channel_id_fb, file_id_clean)

        if channel_id_fb and user_id_fb:
            try:
                sync_client.chat_postEphemeral(
                    channel=channel_id_fb,
                    user=user_id_fb,
                    text="Thanks — recorded your feedback.",
                )
            except Exception:  # noqa: BLE001
                logger.debug("doc_feedback_neg: could not post ephemeral")

    # --- dedup callout card action handlers ---

    @async_app.action("ledgr_dedup_replace")
    async def _dedup_replace(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        await ack()
        action_value = (body.get("actions") or [{}])[0].get("value") or ""
        channel_id_dr = (body.get("channel") or {}).get("id") or ""
        message_ts_dr = (body.get("message") or {}).get("ts") or ""
        try:
            parts = action_value.split("|", 3)
            vendor_raw = urllib.parse.unquote(parts[0]) if len(parts) > 0 else ""
            month_raw = urllib.parse.unquote(parts[2]) if len(parts) > 2 else ""
            stash_key = (
                urllib.parse.unquote(parts[3])
                if len(parts) > 3 and parts[3] not in ("", "-")
                else ""
            )
        except Exception:  # noqa: BLE001
            vendor_raw, month_raw, stash_key = "", "", ""
        label = f"{month_raw} · {vendor_raw}" if (vendor_raw and month_raw) else action_value

        replaced = False
        if stash_key and isinstance(ledger_store, SlackLedgerStore):
            try:
                stash = await asyncio.to_thread(
                    ledger_store.consume_bank_dedup_replace, stash_key,
                )
                if stash and stash.get("batches"):
                    doc_keys = [
                        str(b.get("doc_key") or "")
                        for b in stash["batches"]
                        if b.get("doc_key")
                    ]
                    await asyncio.to_thread(
                        ledger_store.purge_seen_doc_keys,
                        stash["client_id"],
                        stash["fy"],
                        doc_keys,
                    )
                    append_result = await asyncio.to_thread(
                        ledger_store.append_rows,
                        client_id=stash["client_id"],
                        fy=stash["fy"],
                        slack_client=sync_client,
                        channel_id=channel_id_dr,
                        batches=stash["batches"],
                        software=stash.get("software") or "",
                        kind=stash.get("kind") or "bank",
                        client_name=stash.get("client_name") or "",
                    )
                    replaced = int(append_result.get("appended") or 0) > 0
            except Exception:  # noqa: BLE001
                logger.exception("dedup_replace: bank re-merge failed stash=%s", stash_key)

        if channel_id_dr and message_ts_dr:
            try:
                if replaced:
                    outcome = (
                        f"✅ Replaced {label} in your bank statement workbook."
                    )
                    sync_client.chat_update(
                        channel=channel_id_dr,
                        ts=message_ts_dr,
                        text=outcome,
                        blocks=[
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": outcome},
                            }
                        ],
                    )
                else:
                    sync_client.chat_postEphemeral(
                        channel=channel_id_dr,
                        user=(body.get("user") or {}).get("id") or "",
                        text=(
                            f"Will replace {label} — re-upload the file "
                            "to trigger re-processing."
                        ),
                    )
            except Exception:  # noqa: BLE001
                logger.debug("dedup_replace: could not post outcome")

    @async_app.action("ledgr_dedup_keep")
    async def _dedup_keep(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        # No-op on the ledger. Edit the dedup card in-place to a kept-existing outcome line.
        await ack()
        action_value = (body.get("actions") or [{}])[0].get("value") or ""
        channel_id_dk = (body.get("channel") or {}).get("id") or ""
        message_ts_dk = (body.get("message") or {}).get("ts") or ""
        try:
            parts = action_value.split("|", 3)
            vendor_raw = urllib.parse.unquote(parts[0]) if len(parts) > 0 else ""
            month_raw = urllib.parse.unquote(parts[2]) if len(parts) > 2 else ""
        except Exception:  # noqa: BLE001
            vendor_raw, month_raw = "", ""
        label = f"{month_raw} · {vendor_raw}" if (vendor_raw and month_raw) else "existing entry"
        outcome_text = f"✅ Kept existing — {label} unchanged."
        if channel_id_dk and message_ts_dk:
            try:
                sync_client.chat_update(
                    channel=channel_id_dk,
                    ts=message_ts_dk,
                    text=outcome_text,
                    blocks=[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": outcome_text},
                        }
                    ],
                )
            except Exception:  # noqa: BLE001
                logger.debug("dedup_keep: could not update message")

    # --- onboarding + commands (reuse parked sync handlers off-thread) ---

    @async_app.action("ledgr_setup_open")
    async def _setup_open(body, ack, client, context=None):
        sync_client = _sync_client_for(context, client)
        await ack()
        prefill = await _derive_setup_prefill(sync_client, body)
        await asyncio.to_thread(
            handle_setup_open, body, lambda *a, **k: None, sync_client, prefill
        )

    @async_app.view("ledgr_onboarding")
    async def _onboarding(body, ack, client, context=None):
        sync_client = _sync_client_for(context, client)
        await ack()
        await asyncio.to_thread(
            handle_onboarding_submit,
            body,
            lambda *a, **k: None,
            sync_client,
            store,
            lambda: "client-" + os.urandom(6).hex(),
        )

    @async_app.command(ledgr_slash_command_name())
    async def _ledgr(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        await ack()
        await asyncio.to_thread(
            handle_ledgr_command, lambda *a, **k: None, body, sync_client, store
        )

    @async_app.event("member_joined_channel")
    async def _member_joined(event, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        eid = body.get("event_id") or f"{event.get('type')}:{event.get('event_ts') or event.get('ts')}"
        if _seen.seen_before(eid):
            logger.debug("dedup: dropping duplicate member_joined_channel event %s", eid)
            return
        bot_user_id = (context or {}).get("bot_user_id") or ""
        await asyncio.to_thread(handle_member_joined, body, None, sync_client, bot_user_id)

    # --- text-question + file-upload handler ---

    @async_app.event("app_mention")
    async def _app_mention(event, body, client, context=None):
        """Handle @Ledgr mentions explicitly (Slack also sends message.channels)."""
        sync_client = _sync_client_for(context, client)
        eid = body.get("event_id") or f"app_mention:{event.get('ts')}"
        if _seen.seen_before(eid):
            return
        channel_id = event.get("channel")
        if not channel_id:
            return
        text = _strip_slack_mentions(event.get("text") or "")
        if not text:
            text = "Hello"
        await _handle_chat_turn(
            chat_runner=chat_runner,
            ledger_store=ledger_store,
            slack_client=sync_client,
            channel_id=channel_id,
            question=text,
            client_store=store,
            message_ts=event.get("ts"),
            thread_ts=event.get("thread_ts") or event.get("ts"),
            raw_thread_ts=event.get("thread_ts"),
            doc_runner=runner,
            db=db,
        )

    @async_app.event("message")
    async def _message(event, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        # Dedup: Slack socket-mode can redeliver the same event on reconnect.
        # One guard per message event_id covers both the file and text paths so
        # a redelivery of a file_share message is suppressed exactly once.
        eid = body.get("event_id") or f"{event.get('type')}:{event.get('ts')}"
        if _seen.seen_before(eid):
            logger.debug("dedup: dropping duplicate message event %s", eid)
            return

        # Ignore bot messages and edit/delete noise — but still process file
        # uploads posted by this app (files.upload / API tests carry bot_id).
        subtype = event.get("subtype") or ""
        files = event.get("files") or []
        is_file_upload = subtype == "file_share" or bool(files)
        if subtype in ("message_changed", "message_deleted"):
            return
        if subtype == "bot_message" and not is_file_upload:
            return
        if event.get("bot_id") and not is_file_upload:
            return

        channel_id = event.get("channel")
        if not channel_id:
            return

        user_hint = _strip_slack_mentions(event.get("text") or "")

        # File-upload path: message subtype "file_share" OR event carries a
        # "files" list (some Slack app configurations omit the subtype but still
        # include the files array).  Process each file independently; the shared
        # _seen guard above already prevents double-processing if the same
        # event_id is redelivered.
        if subtype == "file_share" or files:
            # ADR-0007: one Job summary message per batch drop, threaded.
            # Post the summary up-front (initial text), pass its ``ts`` as
            # ``thread_ts`` into each ``process_file_event`` so every per-doc
            # status / approval / delivery card lands under it, then edit the
            # summary in-place with the final tally once the loop finishes.
            from app.blocks import job_summary_text

            # Step 5 — Pre-gather de-dup: deduplicate by file id BEFORE fan-out
            # so two list entries sharing an id (file_share + message/file_share
            # dual-fire, or an API test sending duplicates) are processed exactly
            # once.  The _seen.seen_before("file:{id}") guard inside the loop
            # body cannot protect against this under asyncio.gather because two
            # coroutines can both pass the check before either marks the id seen.
            _seen_ids: set[str] = set()
            _deduped_files: list = []
            for _f in files:
                _fid = _f.get("id") if isinstance(_f, dict) else None
                if _fid is None:
                    _deduped_files.append(_f)
                elif _fid not in _seen_ids:
                    _seen_ids.add(_fid)
                    _deduped_files.append(_f)
                else:
                    logger.debug("pre-gather dedup: skipping duplicate file id %s", _fid)
            files = _deduped_files

            total = len(files)
            # Per-doc row tracker for the batch plan block. Each entry is a
            # dict {file_label, stage, detail, status} updated by the per-doc
            # status_callback as the run advances. Initial state: queued.
            doc_rows: list[dict] = []
            for f in files:
                fid = f.get("id") if isinstance(f, dict) else None
                fname = _resolve_file_name(sync_client, fid, f) if fid else ""
                doc_rows.append({
                    "file_label": fname or f"doc {len(doc_rows) + 1}",
                    "stage": "queued",
                    "detail": None,
                    "status": "in_progress",
                })

            # Post the placeholder summary (top-level, no thread_ts) with the
            # BatchKit ``plan`` block listing every document. Single and
            # multi-file drops use the same plan block UX; this matches the
            # intent of ADR-0007 (one Job summary per drop) and keeps the
            # per-doc "thinking" stages visible in the main channel.
            from app.blocks import batch_processing_plan_blocks
            initial_blocks = batch_processing_plan_blocks(
                total=total,
                done=0,
                doc_rows=list(doc_rows),
                channel_id=channel_id,
            )
            initial_text = job_progress_text(total=total, done=0)
            try:
                post_kwargs: dict = {"channel": channel_id, "text": initial_text}
                if initial_blocks:
                    post_kwargs["blocks"] = initial_blocks
                resp = sync_client.chat_postMessage(**post_kwargs)
            except Exception:  # noqa: BLE001 - cosmetic; never abort the upload
                logger.exception("failed to post Job summary in %s", channel_id)
                try:
                    resp = sync_client.chat_postMessage(
                        channel=channel_id,
                        text=initial_text,
                    )
                except Exception:
                    logger.exception("failed to post fallback Job summary in %s", channel_id)
                    resp = None
            summary_ts: Optional[str] = None
            if resp is not None:
                data = resp.data if hasattr(resp, "data") else resp
                if isinstance(data, dict):
                    summary_ts = data.get("ts")

            posted = 0
            needs_review = 0
            rejected = 0
            failed = 0
            duplicates = 0
            done = 0
            software_hint = ""
            fy_hint = ""
            kind_hint = ""
            # Single-file drops now use the same main-channel UX as multi-file:
            # processing "thinking" stages render on the top-level job summary
            # (batch_processing_plan_blocks), and the delivery preview tables
            # merge into the same top-level final edit. HITL review cards
            # continue to thread under summary_ts (ADR-0007).
            batch_defer = True
            batch_deferred: list[dict] = []
            batch_file_ids: list[str] = []
            _last_progress_refresh = [0.0]
            _PROGRESS_REFRESH_MIN_S = 1.5

            def _refresh_job_progress(*, force: bool = False) -> None:
                if not summary_ts:
                    return
                import time as _time
                now = _time.monotonic()
                if (
                    not force
                    and done < total
                    and (now - _last_progress_refresh[0]) < _PROGRESS_REFRESH_MIN_S
                ):
                    return
                _last_progress_refresh[0] = now
                progress_text = job_progress_text(
                    total=total,
                    done=done,
                    posted=posted,
                    needs_review=needs_review,
                    rejected=rejected,
                    failed=failed,
                    duplicates=duplicates,
                )
                try:
                    # Use the plan block for every drop (single + multi) so the
                    # user sees live per-doc thinking on the top-level message.
                    blocks = batch_processing_plan_blocks(
                        total=total,
                        done=done,
                        doc_rows=list(doc_rows),
                        channel_id=channel_id,
                    )
                    sync_client.chat_update(
                        channel=channel_id,
                        ts=summary_ts,
                        text=progress_text,
                        blocks=blocks,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("job progress update failed", exc_info=True)
                    try:
                        sync_client.chat_update(
                            channel=channel_id,
                            ts=summary_ts,
                            text=progress_text,
                        )
                    except Exception:
                        logger.debug("job progress text-only update failed", exc_info=True)

            def _batch_status_cb(update: dict) -> None:
                """Callback fired by process_file_event on each stage change.

                Finds the row matching the doc's filename and updates its
                stage/detail/status in place; then refreshes the placeholder
                with the live plan block. Runs for every drop size — single
                files update the same top-level plan block as multi-file drops.
                """
                label = update.get("file_label") or ""
                for row in doc_rows:
                    if row.get("file_label") == label:
                        row.update({
                            "stage": update.get("stage") or row.get("stage"),
                            "detail": update.get("detail"),
                            "status": update.get("status") or row.get("status"),
                        })
                        break
                _refresh_job_progress()

            # COA-routing path A (ADR-0006): spreadsheets dropped alongside
            # documents are OFFERED as a COA via a confirm card; only non-spreadsheet
            # files (PDFs, images) flow into process_file_event. This replaced the
            # old auto-routing so users explicitly approve COA use (ADR-0006).
            from app.slack_app import _is_spreadsheet

            _resolved = store.get_by_channel(channel_id)

            def _say_in_channel(**kwargs):
                sync_client.chat_postMessage(channel=channel_id, **kwargs)

            # Step 5 — Fan-out: each doc runs concurrently via asyncio.gather.
            # _run_one owns the per-doc pipeline and returns a result record; ALL
            # shared-state mutations (counters, batch_deferred, hints) happen in the
            # post-gather reduce below so parallel coroutines never race on shared
            # mutable state.  No new semaphore is added: process_file_event already
            # acquires _SEM internally, so gather self-bounds concurrency.

            async def _run_one(f: dict, idx: int) -> dict:
                """Run one doc through the pipeline; return a result record.

                Never raises — the entire body is wrapped so one bad doc (even a
                crash in _offer_coa_confirmation or _resolve_file_name) never
                cancels sibling coroutines.  The outer gather uses
                return_exceptions=True as a belt-and-suspenders guard.
                """
                try:
                    file_id = f.get("id") if isinstance(f, dict) else None
                    if not file_id:
                        return {"idx": idx, "status": "skipped", "file_id": None,
                                "append": {}, "fname": ""}

                    # File-level dedup: file_shared + message/file_share both fire
                    # for one upload; guard on the file id so it's processed once.
                    if _seen.seen_before(f"file:{file_id}"):
                        logger.debug("dedup: file %s already being processed", file_id)
                        # Await the Future from the file_shared handler so the batch
                        # tally counts this file's outcome (legacy COA path only).
                        fut = _file_futures.pop(file_id, None)
                        fut_result = None
                        if fut is not None:
                            try:
                                fut_result = await fut
                            except Exception:  # noqa: BLE001
                                logger.debug("file_shared processing failed for %s", file_id)
                        return {"idx": idx, "status": "seen_before",
                                "file_id": file_id, "append": {},
                                "fut_result": fut_result, "fname": ""}

                    logger.info(
                        "file upload received via message: file=%s channel=%s",
                        file_id, channel_id,
                    )

                    # Offer every spreadsheet (any channel state) as a COA candidate.
                    # The confirm card carries the active/replace copy; non-spreadsheet
                    # files (PDFs, images) fall through to the document pipeline.
                    if isinstance(f, dict) and _is_spreadsheet(f):
                        await _offer_coa_confirmation(
                            sync_client=sync_client,
                            channel_id=channel_id,
                            file_id=file_id,
                            file_payload=f,
                            channel_state=_channel_state_label(_resolved),
                        )
                        return {"idx": idx, "status": "coa", "file_id": file_id,
                                "append": {}, "fname": ""}

                    fname = _resolve_file_name(sync_client, file_id, f)
                    # Update the doc_row for this slot — keyed by idx so two docs
                    # with identical filenames (e.g. "document.pdf") each update
                    # their own row without colliding on fname.
                    if 0 <= idx < len(doc_rows):
                        doc_rows[idx].update({"stage": "Starting…", "status": "in_progress"})
                    _refresh_job_progress()

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
                        source_filename=fname,
                        hint=user_hint,
                        defer_slack_delivery=batch_defer,
                        batch_mode=batch_defer,
                        defer_ledger_persist=batch_defer,
                        status_callback=_batch_status_cb if batch_defer else None,
                    )
                    return {"idx": idx, "status": (result or {}).get("status", ""),
                            "file_id": file_id,
                            "append": (result or {}).get("append") or {},
                            "fname": fname}

                except Exception as exc:  # noqa: BLE001 — whole-coroutine safety net
                    file_id_safe = (f.get("id") if isinstance(f, dict) else None) or ""
                    logger.exception("batch file processing failed: file=%s", file_id_safe)
                    err_short = str(exc).split("\n", maxsplit=1)[0][:200]
                    if 0 <= idx < len(doc_rows):
                        doc_rows[idx].update({
                            "stage": "Processing failed",
                            "detail": err_short,
                            "status": "failed",
                        })
                    return {"idx": idx, "status": "processing_failed",
                            "file_id": file_id_safe, "append": {}, "fname": "",
                            "error": err_short}

            # Fan-out: run all docs concurrently.  return_exceptions=True ensures
            # that even if _run_one's outer try/except somehow misses a raise (e.g.
            # a BaseException subclass that bypasses BLE001) the sibling coroutines
            # still complete.  The reduce below converts any leftover Exception
            # objects into processing_failed records.
            _raw_results = await asyncio.gather(
                *[_run_one(f, i) for i, f in enumerate(files)],
                return_exceptions=True,
            )
            _one_results: list[dict] = []
            for _i, _r in enumerate(_raw_results):
                if isinstance(_r, BaseException):
                    logger.error(
                        "gather: unexpected exception from _run_one idx=%d: %s", _i, _r,
                        exc_info=_r,
                    )
                    _one_results.append({
                        "idx": _i, "status": "processing_failed",
                        "file_id": "", "append": {}, "fname": "",
                        "error": str(_r).split("\n", 1)[0][:200],
                    })
                else:
                    _one_results.append(_r)

            # Post-gather reduce: iterate results in ORIGINAL INPUT ORDER and
            # mutate shared aggregates.  This is the only place shared state is
            # written, so there are no races.
            for _res in sorted(_one_results, key=lambda r: r["idx"]):
                _status = _res.get("status") or ""
                _file_id = _res.get("file_id")
                _append = _res.get("append") or {}

                if _status == "skipped":
                    continue

                if _status == "seen_before":
                    # Legacy file_shared path: tally the awaited future's outcome.
                    _fut_result = _res.get("fut_result") or {}
                    _fstatus = _fut_result.get("status")
                    if _fstatus == "delivered":
                        posted += 1
                        _fa = (_fut_result.get("append") or {})
                        if not software_hint and _fa.get("software"):
                            software_hint = str(_fa["software"])
                        if not fy_hint and _fa.get("fy"):
                            fy_hint = str(_fa["fy"])
                        if not kind_hint and _fa.get("kind"):
                            kind_hint = str(_fa["kind"])
                    elif _fstatus == "duplicate":
                        duplicates += 1
                    elif _fstatus == "paused":
                        needs_review += 1
                    elif _fstatus == "rejected_unreadable":
                        rejected += 1
                    done += 1
                    continue

                if _status == "coa":
                    done += 1
                    continue

                # Normal pipeline result.
                done += 1
                if _status == "delivered":
                    posted += 1
                    if _file_id:
                        batch_file_ids.append(_file_id)
                    deferred = _append.get("deferred_delivery")
                    if deferred:
                        batch_deferred.append(deferred)
                    if not software_hint and _append.get("software"):
                        software_hint = str(_append["software"])
                    if not fy_hint and _append.get("fy"):
                        fy_hint = str(_append["fy"])
                    if not kind_hint and _append.get("kind"):
                        kind_hint = str(_append["kind"])
                elif _status == "duplicate":
                    duplicates += 1
                elif _status == "paused":
                    needs_review += 1
                elif _status == "rejected_unreadable":
                    rejected += 1
                elif _status == "processing_failed":
                    failed += 1

            _refresh_job_progress(force=True)

            # Batch-end: merge stashed ledger payloads and write the workbook ONCE
            # per (client, fy, kind) group — applies to single- and multi-file drops
            # whenever ``defer_ledger_persist`` was used.
            flush_results: list[dict] = []
            if batch_deferred and ledger_store is not None:
                flush_results = await _flush_deferred_ledger_writes(
                    ledger_store=ledger_store,
                    slack_client=sync_client,
                    channel_id=channel_id,
                    batch_deferred=batch_deferred,
                )

            ledger_appended = sum(int(r.get("appended") or 0) for r in flush_results)
            ledger_deduped = sum(int(r.get("deduped") or 0) for r in flush_results)

            # Edit the summary in-place with the final tally (ADR-0007). Always
            # merge delivery preview blocks from extracted rows when the batch
            # stashed payloads — independent of whether Firestore deduped at flush.
            if summary_ts:
                try:
                    delivery_summary, agg_blocks = (
                        _build_batch_aggregate_blocks(batch_deferred, channel_id)
                        if batch_deferred else ("", [])
                    )
                    if delivery_summary:
                        final_text = delivery_summary
                        if ledger_deduped > 0 and ledger_appended == 0:
                            final_text += " _(workbook unchanged)_"
                    else:
                        final_text = job_summary_text(
                            total=total,
                            posted=posted,
                            needs_review=needs_review,
                            rejected=rejected,
                            failed=failed,
                            duplicates=duplicates,
                            software=software_hint,
                            fy=fy_hint,
                            kind=kind_hint,
                        )
                    update_kwargs: dict = {
                        "channel": channel_id,
                        "ts": summary_ts,
                        "text": final_text,
                    }
                    if batch_deferred and agg_blocks:
                        blocks_out = list(agg_blocks)
                        # Same-period bank re-drop: surface Replace / Keep callout.
                        if (
                            ledger_appended == 0
                            and ledger_deduped > 0
                            and kind_hint == "bank"
                        ):
                            dedup_blocks, stash_key = _bank_batch_dedup_callout(
                                batch_deferred, flush_results, channel_id,
                            )
                            if dedup_blocks:
                                blocks_out.extend(dedup_blocks)
                                if ledger_store is not None and stash_key:
                                    _stash_bank_dedup_replace(
                                        ledger_store,
                                        batch_deferred,
                                        stash_key=stash_key,
                                    )
                        update_kwargs["blocks"] = blocks_out
                    sync_client.chat_update(**update_kwargs)
                except Exception:  # noqa: BLE001 - cosmetic
                    logger.exception("failed to update Job summary in %s", channel_id)
                # Phase 2 backfill: patch delivery_message_ts onto per-doc log
                # entries written during the batch loop (summary_ts is the thread parent).
                if batch_file_ids and store is not None:
                    profile = store.get_by_channel(channel_id)
                    cid = getattr(profile, "client_id", None) or ""
                    if cid:
                        per_file_meta: list[dict] = []
                        for fid, deferred in zip(
                            batch_file_ids, batch_deferred, strict=False,
                        ):
                            batches = (deferred or {}).get("batches") or []
                            per_file_meta.append({
                                "file_id": fid,
                                "row_count": sum(
                                    len(b.get("rows") or []) for b in batches
                                ),
                                "invoice_ids": _invoice_ids_from_batches(batches),
                            })
                        _patch_processing_log_delivery_ts(
                            store,
                            client_id=cid,
                            channel_id=channel_id,
                            delivery_message_ts=summary_ts,
                            file_ids=batch_file_ids,
                            fy=fy_hint,
                            per_file=per_file_meta,
                        )
            elif batch_deferred:
                # No summary_ts (rare — placeholder post failed) — fall back to a
                # separate delivery post for backwards-compat.
                _post_batch_aggregate_delivery(
                    sync_client, channel_id, batch_deferred,
                )

            return

        # Text-question path: plain user message with no files.
        text = user_hint
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
        await _handle_chat_turn(
            chat_runner=chat_runner,
            ledger_store=ledger_store,
            slack_client=sync_client,
            channel_id=channel_id,
            question=text,
            client_store=store,
            message_ts=message_ts,
            thread_ts=thread_ts,
            raw_thread_ts=raw_thread_ts,
            doc_runner=runner,
            db=db,
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
    from accounting_agents.observability.sentry_trends import init_sentry_if_configured
    from fastapi import FastAPI

    init_sentry_if_configured()
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
