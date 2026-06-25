"""Chat assistant instructions and ADK callbacks."""

from __future__ import annotations

import logging
import re

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse

from .constants import (
    PENDING_REVIEWS_KEY,
    PROCESSING_LOG_KEY,
    THREAD_FOCUS_KEY,
)

logger = logging.getLogger(__name__)

# Shared session-state preamble (ADK ``{+key?+}`` placeholders).
_SHARED_PREAMBLE = """
{+onboarding_gate?+}
The client's jurisdiction (region + tax system + currency) is filled in by ADK
from session state via {+region?+} / {+base_currency?+} / {+tax_system?+}
templating — apply the correct tax rules per jurisdiction (Singapore GST vs
Malaysia SST vs cross-border). Never assume Singapore 9% GST for a non-SG
client.

Preamble (filled by ADK from session state — see runner state_delta):
- client: {+client_name+} (UEN {+client_uen?+}, {+region?+}, base {+base_currency?+},
  tax-registered: {+tax_registered?+}, tax system: {+tax_system?+},
  FYE month {+fye_month?+})
- loaded FY: FY{+fy_loaded?+}  ({+ledger_row_count?+} rows)
- Processing history: {+processing_log_count?+} deliveries
- Pending reviews: {+pending_review_count?+} awaiting approval
- Thread-scoped delivery (Phase 3 — only set when the user replies under a
  delivery card; the runner derives these from processing_log + the parent
  message ts of the thread):
  * message_ts: {+thread_delivery_message_ts?+}
  * filenames:   {+thread_delivery_filenames?+}
  * invoice_ids: {+thread_delivery_invoice_ids?+}
  * FY of the delivery: FY{+thread_delivery_fy?+}
  * rows shown in the delivery card table: {+thread_delivery_preview_rows?+}
  * pre-resolved ledger matches (runner prefetch): {+thread_delivery_ledger_matches?+}
  * thread focus from the last account-code answer: {+thread_focus?+}
""".strip()

# Backward-compatible export — tests and ``assistant_instruction`` render this.
_BASE_INSTRUCTION = (
    _SHARED_PREAMBLE
    + """

You are the read-only accounting assistant for an SME's FY ledger.
Answer strictly from the data already loaded into your session — never invent
figures, never call external services. The data may be an INVOICE ledger or a
BANK STATEMENT; each tool's docstring tells you which kind it expects.

For every question, call the single most relevant tool first, then explain the
result in plain English. If a tool reports the data is not loaded, call
``diagnose_assistant_context`` before claiming nothing is there.

CRITICAL — ALWAYS finish your turn with a short plain-English message to the
user summarising the answer in your own words and citing the relevant numbers.
NEVER end your turn with only a tool call and no text reply — the user cannot
see raw tool output, so silence looks like a broken assistant.

Be concise and professional.
"""
).strip()

_ROOT_INSTRUCTION = (
    _SHARED_PREAMBLE
    + """

You are the accounting chat coordinator for an SME's FY ledger. Route each user
turn to the right specialist:

- **ledger_analyst** — questions, lookups, summaries, explanations, processing
  history, COA definitions, and diagnostics. Default for most turns.
- **ledger_corrections** — ONLY when the user wants to amend/remove ledger rows,
  clear a month, re-extract a document, or teach a vendor mapping.

Never invent figures. Always finish with a short plain-English answer after any
tool work — the user cannot see raw tool output.

Be concise and professional.
"""
).strip()

_CORRECTIONS_INSTRUCTION = (
    _SHARED_PREAMBLE
    + """

You are the ledger-corrections specialist. You may ONLY use your write tools
after the user explicitly confirms a preview.

MUST / MUST NOT (safety — do not loosen):
- MUST call ``lookup_row`` first to resolve ``row_index`` before amend/remove.
- MUST NOT skip the two-turn confirmation on amend, remove, replace month, or
  re-extract — the first call previews; nothing writes until the user confirms.
- MUST NOT amend bank-sheet rows or non-QBS workbook layouts — refuse clearly.
- ``learn_mapping`` is a direct write (the user's imperative IS the action) —
  no ADK confirmation step; queue to ``pending_learn_mapping`` only.

After any tool, reply in plain English summarising what happened.
"""
).strip()


def _enrich_instruction_state(state_dict: dict) -> dict:
    """Ensure flat count keys exist for ADK ``{+key?+}`` instruction placeholders."""
    enriched = dict(state_dict)
    if enriched.get("processing_log_count") is None:
        plog = enriched.get(PROCESSING_LOG_KEY) or enriched.get("processing_log")
        if isinstance(plog, list):
            enriched["processing_log_count"] = len(plog)
    if enriched.get("pending_review_count") is None:
        pending = enriched.get(PENDING_REVIEWS_KEY) or enriched.get("pending_reviews")
        if isinstance(pending, list):
            enriched["pending_review_count"] = len(pending)

    if enriched.get("onboarding_required") and not enriched.get("software"):
        enriched["onboarding_gate"] = (
            "⚠️ This channel is not onboarded yet. The user must run /ledgr settings first. "
            "Tell them this — do NOT attempt to answer ledger questions."
        )

    if not enriched.get("tax_system"):
        region = (enriched.get("client_region") or enriched.get("region") or "").strip().upper()
        if region in ("SINGAPORE", "SG", "SGP"):
            enriched["tax_system"] = "GST"
        elif region in ("MALAYSIA", "MY", "MYS", "MSIA"):
            enriched["tax_system"] = "SST"
    return enriched


def assistant_instruction(ctx) -> str:
    """Render ``_BASE_INSTRUCTION`` with ADK's session-state injection."""
    try:
        state_obj = ctx.state
        state_dict = _enrich_instruction_state(
            dict(state_obj) if hasattr(state_obj, "items") else {}
        )
    except Exception:  # noqa: BLE001
        return _BASE_INSTRUCTION

    def _sub(match: re.Match[str]) -> str:
        raw = match.group(0).lstrip("{").rstrip("}").strip().lstrip("+").rstrip("+").strip()
        optional = raw.endswith("?")
        key = raw[:-1] if optional else raw
        val = state_dict.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            return "" if optional else match.group(0)
        return str(val)

    try:
        return re.sub(
            r"\{\+[a-zA-Z_][a-zA-Z0-9_]*\?\+\}|\{\+[a-zA-Z_][a-zA-Z0-9_]*\+\}",
            _sub,
            _BASE_INSTRUCTION,
        )
    except Exception:  # noqa: BLE001
        return _BASE_INSTRUCTION


def _assistant_before_agent(callback_context: CallbackContext) -> None:
    """Lazy-load and invoke ``load_client_profile`` to seed client/ledger state."""
    from accounting_agents.agent import load_client_profile

    load_client_profile(callback_context)


def _thread_account_before_model(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """Inject thread-focus preamble when the user asks about a posted account code."""
    try:
        state = callback_context.state
    except Exception:  # noqa: BLE001
        return None

    has_thread = bool(
        state.get("thread_delivery_message_ts")
        or state.get("thread_delivery_ledger_matches")
        or state.get(THREAD_FOCUS_KEY)
    )
    if not has_thread:
        return None

    focus = state.get(THREAD_FOCUS_KEY) or {}
    matches = state.get("thread_delivery_ledger_matches") or []
    inv = ""
    acct = ""
    if isinstance(focus, dict):
        inv = str(focus.get("invoice_id") or "")
        acct = str(focus.get("account_code") or "")
    if not acct and isinstance(matches, list) and matches:
        first = matches[0] if isinstance(matches[0], dict) else {}
        inv = inv or str(first.get("invoice_id") or "")
        acct = acct or str(first.get("account_code") or "")

    if not inv and not acct:
        return None

    preamble = (
        "Thread delivery context is loaded. "
        f"Invoice {inv or '(see thread)'} was posted to account code {acct or '(see ledger matches)'}. "
        "Do NOT ask the user for vendor, line description, or account code. "
        "Call lookup_coa_account or explain_posted_line first, then answer in plain English."
    )
    try:
        llm_request.append_instructions([preamble])
    except Exception:  # noqa: BLE001
        logger.debug("thread account preamble injection failed", exc_info=True)
    return None


def _chat_before_model(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """Thread account/COA preamble injection."""
    return _thread_account_before_model(callback_context, llm_request)
