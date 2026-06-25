"""ADK after_tool_callback: fail-loud policy-enforcement guard.

This callback is registered on the ``root_agent`` and fires after every tool
call.  It acts as the agent-boundary guard for accounting-policy correctness:

- For any tool *other than* ``process_document_batch`` it is a no-op
  (returns ``None`` so ADK keeps the original response unchanged).
- For ``process_document_batch`` it inspects the returned ``BatchResult`` dict
  for hard policy violations that were not already reflected in ``status``.

Fail-loud semantics
-------------------
"Fail loud" means the violation is SURFACED, not silently dropped.  It does
**not** mean crashing the batch and losing processed work.

"Already reflected" means ``status == "needs_review"``.  For ``"blocked"``
there are no extraction-time violations so no hard violation will be present;
``"partial"`` and ``"error"`` describe engine-level outcomes UNRELATED to
policy, so a hard violation in ``review_requests`` is NOT already reflected
by those statuses.

An UNHANDLED hard violation = a hard-id violation is present in
``review_requests`` AND ``status`` is NOT ``"needs_review"``.

If an unhandled hard violation is detected the callback:

- In **STRICT mode** (``LEDGR_VALIDATE_STRICT`` env var set to a truthy
  value such as ``"1"`` or ``"true"``): raises ``ValueError`` immediately.
  This is intended for CI and automated eval runs where a silent mis-coding
  should block the pipeline.  Applies to ALL statuses (success, partial, error).
- In **normal mode**: returns a *copy* of the response dict with:
  - ``validation_summary["policy_enforcement"]`` set to
    ``"failed_open_detected"``
  - ``status`` set to ``"needs_review"`` ONLY when the current status is
    ``"success"``.  ``"partial"`` and ``"error"`` are NOT downgraded — their
    status is kept; only the annotation is added.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Optional

from ledgr_agent.policies.constants import HARD_VIOLATION_IDS


def _is_strict() -> bool:
    """Return True when LEDGR_VALIDATE_STRICT is set to a truthy value."""
    val = os.environ.get("LEDGR_VALIDATE_STRICT", "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _has_unhandled_hard_violation(tool_response: dict[str, Any]) -> bool:
    """Return True when the response contains a hard violation not yet reflected in status.

    "Already reflected" means ``status == "needs_review"`` only.
    ``"partial"`` and ``"error"`` describe engine-level outcomes unrelated to
    policy — a hard violation present there is unhandled and must be surfaced.
    ``"blocked"`` will naturally have no extraction-time violations so it falls
    through to False without special-casing.
    """
    status = tool_response.get("status")
    if status == "needs_review":
        # Hard violation (if any) is already reflected in this status.
        return False

    review_requests = tool_response.get("review_requests") or []
    for req in review_requests:
        if not isinstance(req, dict):
            continue
        if req.get("severity") == "hard_review" and req.get("id") in HARD_VIOLATION_IDS:
            return True
    return False


def validate_output_after_tool(
    tool: Any,
    args: Any,  # noqa: ARG001
    tool_context: Any,  # noqa: ARG001
    tool_response: Any,
) -> Optional[dict[str, Any]]:
    """ADK ``after_tool_callback``: surface hard policy violations.

    Returns ``None`` (keep original response) unless a hard policy violation
    is detected in the ``process_document_batch`` response that has not yet
    been reflected in ``status``.

    See module docstring for full fail-loud semantics.
    """
    # Pass-through: only act on process_document_batch.
    if getattr(tool, "name", None) != "process_document_batch":
        return None

    if not isinstance(tool_response, dict):
        return None

    if not _has_unhandled_hard_violation(tool_response):
        return None

    # Hard violation detected and not yet reflected in status — fail loud.
    if _is_strict():
        # Collect violation ids for the error message.
        ids = [
            req.get("id", "?")
            for req in (tool_response.get("review_requests") or [])
            if isinstance(req, dict)
            and req.get("severity") == "hard_review"
            and req.get("id") in HARD_VIOLATION_IDS
        ]
        raise ValueError(
            f"LEDGR_VALIDATE_STRICT: hard policy violation(s) detected but "
            f"status was {tool_response.get('status')!r} — {ids}"
        )

    # Normal mode: annotate validation_summary.
    # Only flip status to "needs_review" when it was "success" — do NOT
    # downgrade "partial" or "error" (they carry independent meaning).
    patched = copy.deepcopy(tool_response)
    if patched.get("status") == "success":
        patched["status"] = "needs_review"
    validation_summary = patched.get("validation_summary")
    if not isinstance(validation_summary, dict):
        validation_summary = {}
    validation_summary["policy_enforcement"] = "failed_open_detected"
    patched["validation_summary"] = validation_summary
    return patched
