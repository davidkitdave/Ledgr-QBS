"""HITL (human-in-the-loop) correlation + resume helpers.

The :func:`approval_gate <accounting_agents.nodes.approval_gate>` node pauses a
resumable workflow by yielding a ``RequestInput``. This module is the bridge
between that paused ADK invocation and the outside world (the Slack Bolt layer in
Task #8):

* :func:`write_interrupt` / :func:`read_interrupt` persist a small Firestore
  correlation doc (``interrupts/{op_id}``) carrying everything the resume needs:
  the ``session_id``, ``channel_id``, ``slack_file_id`` and ``message_ts``.
* :func:`resume_session` loads that doc and feeds the human's
  :class:`ApproveDecision <accounting_agents.nodes.ApproveDecision>` back into
  ``runner.run_async`` using the exact resume payload verified against ADK
  2.2.0: a ``types.Content`` whose single part is a ``FunctionResponse`` with
  ``id == interrupt_id`` and ``name == 'adk_request_input'``.
* :func:`mark_processed` / :func:`is_processed` provide a ``processed/{op_id}``
  idempotency marker so a double-click / double-delivery resumes the workflow at
  most once.

This module is deliberately Slack-agnostic; the Bolt handler in Task #8 calls
these helpers. The Firestore client is injectable for hermetic tests.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.runners import Runner
from google.genai import types

from .nodes import ApproveDecision

logger = logging.getLogger(__name__)

#: Firestore collection for pending-approval correlation docs.
INTERRUPTS_COLLECTION = "interrupts"

#: Firestore collection for idempotency markers.
PROCESSED_COLLECTION = "processed"

#: The ADK function-call name a ``RequestInput`` surfaces as (ground truth from
#: ``google/adk/workflow/utils/_workflow_hitl_utils.py``).
REQUEST_INPUT_FUNCTION_CALL_NAME = "adk_request_input"


# --------------------------------------------------------------------------- #
# Interrupt correlation doc
# --------------------------------------------------------------------------- #


def write_interrupt(
    db: Any,
    op_id: str,
    *,
    session_id: str,
    channel_id: str,
    slack_file_id: str,
    message_ts: Optional[str] = None,
    status: str = "pending",
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Persist the correlation doc for a paused workflow.

    Args:
        db: A Firestore client (or compatible fake).
        op_id: The interrupt id used by ``approval_gate`` (== the resume key).
        session_id: ADK session id (== channel id, by convention).
        channel_id: Slack channel the document was dropped in.
        slack_file_id: Slack file id (re-download the PDF on resume if needed).
        message_ts: Timestamp of the Slack approval card (for updating it).
        status: Lifecycle status; starts ``"pending"``.
        extra: Optional additional fields to merge into the doc.
    """
    doc: dict[str, Any] = {
        "op_id": op_id,
        "session_id": session_id,
        "channel_id": channel_id,
        "slack_file_id": slack_file_id,
        "message_ts": message_ts,
        "status": status,
    }
    if extra:
        doc.update(extra)
    db.collection(INTERRUPTS_COLLECTION).document(op_id).set(doc)


def read_interrupt(db: Any, op_id: str) -> Optional[dict[str, Any]]:
    """Return the correlation doc for ``op_id``, or ``None`` if absent."""
    snap = db.collection(INTERRUPTS_COLLECTION).document(op_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


def update_interrupt_status(db: Any, op_id: str, status: str) -> None:
    """Patch the correlation doc's ``status`` (e.g. ``"resolved"``)."""
    db.collection(INTERRUPTS_COLLECTION).document(op_id).set(
        {"status": status}, merge=True
    )


# --------------------------------------------------------------------------- #
# Idempotency marker
# --------------------------------------------------------------------------- #


def is_processed(db: Any, op_id: str) -> bool:
    """Return ``True`` if ``op_id`` has already been resumed/processed."""
    return db.collection(PROCESSED_COLLECTION).document(op_id).get().exists


def mark_processed(db: Any, op_id: str) -> None:
    """Record the ``processed/{op_id}`` marker so a double-resume is a no-op."""
    db.collection(PROCESSED_COLLECTION).document(op_id).set({"op_id": op_id})


# --------------------------------------------------------------------------- #
# Resume payload + driver
# --------------------------------------------------------------------------- #


def build_resume_message(op_id: str, decision: ApproveDecision) -> types.Content:
    """Build the ADK resume payload for a paused ``RequestInput``.

    The shape is verified against ADK 2.2.0 (and proven with a live runner
    harness): a single ``FunctionResponse`` part whose ``id`` matches the
    interrupt id and whose ``name`` is ``adk_request_input``. The response body
    is the serialized decision dict, which the framework delivers to the gate's
    successor node as its ``node_input``.
    """
    return types.Content(
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=op_id,
                    name=REQUEST_INPUT_FUNCTION_CALL_NAME,
                    response=decision.model_dump(),
                )
            )
        ]
    )


async def resume_session(
    runner: Runner,
    db: Any,
    op_id: str,
    decision: ApproveDecision,
) -> list[Any]:
    """Resume a paused workflow with the human's decision (idempotent).

    Loads the correlation doc for ``op_id``, then drives
    ``runner.run_async(user_id=<session>, session_id=<session>, new_message=<FR>)``.
    Guarded by the ``processed/{op_id}`` marker so a double-resume is a no-op
    (returns an empty event list without touching the runner).

    Args:
        runner: An ADK ``Runner`` bound to the (Firestore-backed) session
            service that holds the paused session.
        db: Firestore client (or fake) holding the interrupt + processed docs.
        op_id: The interrupt id of the paused workflow.
        decision: The human accountant's :class:`ApproveDecision`.

    Returns:
        The list of events emitted while resuming (empty if already processed).

    Raises:
        KeyError: If no correlation doc exists for ``op_id``.
    """
    if is_processed(db, op_id):
        logger.info("resume_session: op_id %s already processed; skipping.", op_id)
        return []

    interrupt = read_interrupt(db, op_id)
    if interrupt is None:
        raise KeyError(f"No interrupt correlation doc for op_id {op_id!r}.")

    session_id = interrupt["session_id"]
    message = build_resume_message(op_id, decision)

    events: list[Any] = []
    async for event in runner.run_async(
        user_id=session_id,
        session_id=session_id,
        new_message=message,
    ):
        events.append(event)

    mark_processed(db, op_id)
    update_interrupt_status(db, op_id, "resolved")
    return events
