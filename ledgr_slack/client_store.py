"""Firestore client profile helpers for Slack."""

from __future__ import annotations

import logging
from typing import Any, Optional

from ledgr_slack.client_context import FirestoreClientStore
from ledgr_slack.credit_adapter import resolve_firm_id_from_client
from ledgr_slack.ux import _post_message

logger = logging.getLogger(__name__)

_DEFAULT_CLIENT_STORE = FirestoreClientStore()

_DOCUMENT_ONLY_REPLY = (
    "I process documents — upload a PDF, PNG, JPG, or WEBP and I'll read it "
    "and build your workbook rows."
)


def _profile_state_delta(client_store, channel_id: str) -> dict:
    """Return the client's ``to_state()`` keys for seeding the run, or ``{}``.

    The coordinator's ``before_agent_callback`` does not reliably propagate the
    profile into the document lane, so the runner seeds it directly at run start
    (alongside ``channel_id``). Empty dict means "no profile for this channel" —
    callers soft-gate on that.

    Also injects firm_id when the client profile resolves one.
    """
    ctx = client_store.get_by_channel(channel_id)
    if ctx is None:
        return {}
    delta = ctx.to_state()
    resolved_firm = resolve_firm_id_from_client(ctx)
    if resolved_firm:
        delta["firm_id"] = resolved_firm
    return delta


def _reply_document_only(
    slack_client: Any,
    channel_id: str,
    *,
    thread_ts: Optional[str] = None,
) -> None:
    _post_message(slack_client, channel_id, _DOCUMENT_ONLY_REPLY, thread_ts=thread_ts)


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
    _SUFFIX_CASE = {
        "pte": "Pte", "ltd": "Ltd", "inc": "Inc", "co": "Co",
        "llp": "LLP", "llc": "LLC", "plc": "PLC",
        "sg": "SG", "my": "MY",
    }
    return " ".join(_SUFFIX_CASE.get(w.lower(), w[:1].upper() + w[1:].lower()) for w in words)
