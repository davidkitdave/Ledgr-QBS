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
from typing import Any, Optional

# FastAPI Request/Response imported at module level so FastAPI can resolve
# the string annotations produced by `from __future__ import annotations`.
from fastapi import Request, Response

from accounting_agents.assistant import (
    LEDGER_DATA_KEY,
    PENDING_LEARN_KEY,
    PENDING_REEXTRACT_KEY,
    PENDING_WRITE_KEY,
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
    proactive_redo_blocks,
    proactive_redo_modal,
    processing_plan_blocks,
    review_card_blocks,
    review_hint_modal,
    review_outcome_blocks,
)
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


#: Maps graph node name → canonical pipeline stage key used by _StageState.
_STAGE_KEY_MAP: dict[str, str] = {
    "classify_node": "classify",
    "extract_invoice_node": "extract",
    "extract_bank_node": "extract",
    "categorize_node": "categorize",
    "tax_node": "tax",
    "approval_gate": "approve",
}


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

    def snapshot(self) -> list[dict]:
        """Return a copy of the current stage list."""
        return [dict(s) for s in self._stages]


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
    replace: bool = False,
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
            replace=effective_replace,
        )
        # Carry context forward so the batch tally can label the destination
        # accurately (bank statement vs ledger) without re-reading the payload.
        append_result.setdefault("kind", payload.get("kind") or "invoice")
        append_result.setdefault("software", payload.get("software") or "")
        append_result.setdefault("fy", str(payload.get("fy") or ""))

    # When every batch was deduped (already in seen_doc_keys), post a native
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
    _post_message(slack_client, channel_id, summary, thread_ts=thread_ts)

    # Post one data_table preview per batch (best-effort; never breaks delivery).
    # Each batch maps to one .xlsx tab, so one preview message per batch gives
    # the user a per-tab window on exactly the rows SlackLedgerStore appended.
    if append_result.get("appended", 0) > 0 and batches:
        workbook_name = append_result.get("filename") or "Ledger.xlsx"
        fy_str = append_result.get("fy") or str(payload.get("fy") or "")
        try:
            fy_int = int(fy_str)
        except (TypeError, ValueError):
            fy_int = 0
        software = str(payload.get("software") or "qbs_ledger")
        for batch in batches:
            batch_rows = batch.get("rows") or []
            if not batch_rows:
                continue
            sheet = str(batch.get("sheet") or "Purchase")
            try:
                preview_blocks = ledger_preview_data_table(
                    rows=batch_rows,
                    workbook_name=workbook_name,
                    fy=fy_int,
                    sheet=sheet,
                    software=software,
                    channel_id=channel_id,
                )
                if preview_blocks:
                    preview_kwargs: dict = {
                        "channel": channel_id,
                        "text": f"Ledger preview — {sheet} ({workbook_name})",
                        "blocks": preview_blocks,
                    }
                    if thread_ts:
                        preview_kwargs["thread_ts"] = thread_ts
                    slack_client.chat_postMessage(**preview_kwargs)
            except Exception:  # noqa: BLE001 — preview is cosmetic; never break delivery
                logger.warning(
                    "ledger preview post failed for sheet %s (non-fatal)", sheet, exc_info=True
                )

    return append_result


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
    _stage_state = _StageState()
    # plan_label is used for ALL processing_plan_blocks calls in this run so the
    # plan block title stays consistent as the status message is edited in place.
    plan_label = f"{_env_prefix()}{source_filename}"
    status_ts = _post_status(
        slack_client,
        channel_id,
        f"{_env_prefix()}📥 Received `{source_filename}` — on it…",
        thread_ts,
        blocks=processing_plan_blocks(
            plan_label,
            stages=_stage_state.snapshot(),
            channel_id=channel_id,
        ),
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
            _stage_state.mark_failed("classify", "Couldn't read this file")
            _update_status(
                slack_client,
                channel_id,
                status_ts,
                "❌ Couldn't read this file",
                blocks=processing_plan_blocks(
                    plan_label,
                    stages=_stage_state.snapshot(),
                    channel_id=channel_id,
                ),
            )
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
            stage_key = event_stage_key(event)
            if stage is not None and stage != last_stage:
                last_stage = stage
                if stage_key is not None:
                    _stage_state.advance(stage_key)
                _update_status(
                    slack_client,
                    channel_id,
                    status_ts,
                    stage,
                    blocks=processing_plan_blocks(
                        plan_label,
                        stages=_stage_state.snapshot(),
                        channel_id=channel_id,
                    ),
                )
            iid = find_interrupt_id(event)
            if iid is not None:
                interrupt_id = iid
            text = extract_final_text(event)
            if text:
                last_text = text

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
        )

        if outcome["status"] == "paused":
            _stage_state.advance("approve")
            _update_status(
                slack_client,
                channel_id,
                status_ts,
                "⏳ Needs your review",
                blocks=processing_plan_blocks(
                    plan_label,
                    stages=_stage_state.snapshot(),
                    channel_id=channel_id,
                ),
            )
            return outcome

        # Delivery branch — decorate the status line and reactions.
        append_result = outcome.get("append", {})
        if append_result.get("all_deduped"):
            _stage_state.mark_complete()
            _update_status(
                slack_client,
                channel_id,
                status_ts,
                "📋 Already recorded",
                blocks=processing_plan_blocks(
                    plan_label,
                    stages=_stage_state.snapshot(),
                    channel_id=channel_id,
                ),
            )
            _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
            _add_reaction(slack_client, channel_id, upload_msg_ts, "ballot_box_with_check")
            return {"status": "duplicate", "append": append_result}

        # Final state: collapse the evolving status to a terminal ✅. The full
        # delivery summary is posted by persist_and_deliver, so keep this short
        # to avoid double-posting the same detail.
        _stage_state.mark_complete()
        _update_status(
            slack_client,
            channel_id,
            status_ts,
            "✅ Processed",
            blocks=processing_plan_blocks(
                plan_label,
                stages=_stage_state.snapshot(),
                channel_id=channel_id,
            ),
        )
        # Swap the 👀 reaction for ✅ on the user's original upload message.
        _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
        _add_reaction(slack_client, channel_id, upload_msg_ts, "white_check_mark")
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
        )
    else:
        update_interrupt_status(db, op_id, "rejected")
        _post_message(slack_client, channel_id, "Document rejected — nothing was added to the ledger.", thread_ts=thread_ts)

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
    )
    return {"status": "resumed", "op_id": op_id, "outcome": outcome, "events": len(events)}


# --------------------------------------------------------------------------- #
# Text-question → Q&A path
# --------------------------------------------------------------------------- #


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
            {LEDGER_DATA_KEY: ledger_rows},
        )

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
        # COA-routing path B (ADR-0006): mirror the message-handler gate so that
        # drag-dropped xlsx/csv files land in run_coa_ingest when the channel is
        # not-yet-onboarded / pending_coa.  Without this check _is_coa_upload is
        # never consulted on the file_shared path and .xlsx is rejected by the
        # global _ACCEPTED_EXTENSIONS allow-list in process_file_event (P0-1).
        _resolved = store.get_by_channel(channel_id)
        _coa_pending = _resolved is None or getattr(_resolved, "status", None) == "pending_coa"
        file_payload = event.get("file") or {"id": file_id}

        if _is_coa_upload(file_payload, coa_pending=_coa_pending):
            logger.info(
                "file_shared: routing spreadsheet %s to COA ingest for channel %s",
                file_id, channel_id,
            )
            import shutil as _shutil
            import tempfile as _tempfile

            from app.slack_app import run_coa_ingest as _run_coa_ingest
            from app.slack_app import slack_download_file as _dl

            def _say_in_channel(**kwargs):
                sync_client.chat_postMessage(channel=channel_id, **kwargs)

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

    # --- mid-flow extract-review HITL (review_extraction_node) ---

    async def _run_review_action(ack, body, action_str, hint=None):
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
    async def _review_confirm(ack, body, client):
        await _run_review_action(ack, body, "confirm_as_is")

    @async_app.action("review_reject")
    async def _review_reject(ack, body, client):
        await _run_review_action(ack, body, "reject")

    @async_app.action("review_reextract")
    async def _review_reextract(ack, body, client):
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
    async def _review_hint_submit(ack, body, client):
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
    async def _proactive_redo(ack, body, client):
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

    @async_app.view("ledgr_proactive_redo")
    async def _proactive_redo_submit(ack, body, client):
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
    async def _per_doc_reextract(ack, body, client):
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
    async def _per_doc_edit(ack, body, client):
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
    async def _per_doc_view_row(ack, body, client):
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
    async def _doc_feedback(ack, body, client):
        """Native context_actions feedback_buttons handler.

        Button value format: ``pos|<doc_ref>`` or ``neg|<doc_ref>``
        where doc_ref = ``file_id|vendor|account_code|tax_code`` (%-encoded fields).
        """
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
    async def _doc_feedback_pos(ack, body, client):
        """Fallback 👍 handler (actions block path)."""
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
    async def _doc_feedback_neg(ack, body, client):
        """Fallback 👎 handler (actions block path)."""
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
    async def _dedup_replace(ack, body, client):
        # User explicitly clicked Replace — queue the replace intent as PENDING_REPLACE_MONTH
        # so the pipeline can drain it on the next available turn (same pattern as
        # PENDING_LEARN_KEY). This avoids unwinding the two-turn chat-tool confirm flow.
        await ack()
        action_value = (body.get("actions") or [{}])[0].get("value") or ""
        channel_id_dr = (body.get("channel") or {}).get("id") or ""
        message_ts_dr = (body.get("message") or {}).get("ts") or ""
        try:
            parts = action_value.split("|", 3)
            vendor_raw = urllib.parse.unquote(parts[0]) if len(parts) > 0 else ""
            month_raw = urllib.parse.unquote(parts[2]) if len(parts) > 2 else ""
        except Exception:  # noqa: BLE001
            vendor_raw, month_raw = "", ""
        label = f"{month_raw} · {vendor_raw}" if (vendor_raw and month_raw) else action_value
        if channel_id_dr and message_ts_dr:
            try:
                sync_client.chat_postEphemeral(
                    channel=channel_id_dr,
                    user=(body.get("user") or {}).get("id") or "",
                    text=f"Will replace {label} — re-upload the file to trigger re-processing.",
                )
            except Exception:  # noqa: BLE001
                logger.debug("dedup_replace: could not post ephemeral")

    @async_app.action("ledgr_dedup_keep")
    async def _dedup_keep(ack, body, client):
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
