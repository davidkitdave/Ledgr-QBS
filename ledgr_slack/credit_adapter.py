"""Slack-side credit gate and charge-on-delivery (Plan 6 / D.2).

Credits are checked before any LLM work and deducted only after a successful
ledger append. The clean-agent tool path skips in-tool deduction unless
``LEDGR_CHARGE_CREDITS_IN_TOOL=1`` so Slack owns billing during cutover.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ledgr_agent.billing import (
    apply_dev_credit_grants_from_env,
    configure_durable_credit_service_if_prod,
    credit_gate_decision,
    delivery_idempotency_key as billing_delivery_idempotency_key,
    get_shared_credit_service,
)
from ledgr_slack.credits_view import (
    credit_footer_block as _coin_credit_footer_block,
    format_coin_footer,
    format_dedup_credit_line,
)

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def resolve_firm_id_from_client(ctx: Any) -> str | None:
    """Firm id for billing: explicit ``firm_id`` or workspace ``slack_team_id``."""

    if ctx is None:
        return None
    for attr in ("firm_id", "slack_team_id"):
        val = getattr(ctx, attr, None)
        if val and str(val).strip():
            return str(val).strip()
    return None


def resolve_firm_id_from_state(state: dict | None) -> str | None:
    if not state:
        return None
    for key in ("firm_id", "slack_team_id"):
        val = state.get(key)
        if val and str(val).strip():
            return str(val).strip()
    return None


def charge_credits_in_tool_enabled() -> bool:
    raw = os.environ.get("LEDGR_CHARGE_CREDITS_IN_TOOL", "")
    return raw.strip().lower() in _TRUTHY


def require_firm_for_billing() -> bool:
    """``LEDGR_CREDIT_REQUIRE_FIRM`` truthy → block uploads with no resolvable firm."""

    raw = os.environ.get("LEDGR_CREDIT_REQUIRE_FIRM", "")
    return raw.strip().lower() in _TRUTHY


def flag_unresolved_firm_billing_anomaly(
    *,
    channel_id: str,
    file_id: str | None,
    source_filename: str | None,
) -> None:
    """LOUD billing-anomaly alert for a real upload with no resolvable firm_id.

    The previous behaviour silently skipped the credit gate when ``firm_id`` was
    falsy, so unbilled documents passed through unnoticed.  This logs at ERROR so
    Cloud Logging / alerting surfaces it.  Call only on the LIVE Slack path; the
    playground/eval path (tool_context=None) legitimately has no firm.
    """

    logger.error(
        "billing anomaly: could not resolve firm_id for live upload — document "
        "will NOT be billed; channel=%s file=%s filename=%s",
        channel_id,
        file_id,
        source_filename,
    )


def wire_shared_credit_service() -> None:
    """Point Slack delivery at the same store as ledgr_agent billing."""

    configure_durable_credit_service_if_prod()
    apply_dev_credit_grants_from_env()


def estimate_upload_pages(data: bytes, filename: str) -> int:
    from ledgr_agent.internal.gemini import count_input_pages, mime_for

    try:
        return max(count_input_pages(data, mime_for(filename)), 1)
    except Exception:  # noqa: BLE001
        return 1


def credit_gate_for_bytes(*, firm_id: str, data: bytes, filename: str) -> dict[str, Any]:
    """Pre-flight gate using page count (bank) or document estimate (default 1)."""

    required = estimate_upload_pages(data, filename)
    return credit_gate_decision(firm_id=firm_id, required_units=required)


def credit_block_message(decision: dict[str, Any]) -> str:
    reason = str(decision.get("reason") or "zero_credit")
    remaining = decision.get("balance")
    if reason == "insufficient_credit":
        return (
            "You don't have enough credits to process this document. "
            f"Balance: {remaining if remaining is not None else 'unknown'}."
        )
    return (
        "🪙 You're out of credits — add more before dropping documents. "
        "Open *Ledgr* in the sidebar → *Home*, or run `/ledgr credits` to check your balance."
    )


def delivery_idempotency_key(*, channel_id: str, file_id: str) -> str:
    return billing_delivery_idempotency_key(channel_id=channel_id, file_id=file_id)


def delivery_charge_units(
    *,
    kind: str,
    payload: dict,
    append_result: dict,
    input_page_count: int | None = None,
) -> int:
    """Units to bill after delivery (ADR-0016 / credit plan slice 3)."""

    if append_result.get("all_deduped"):
        return 0
    appended = int(append_result.get("appended") or 0)
    if appended <= 0:
        return 0

    if kind == "bank":
        pages = input_page_count or payload.get("input_page_count")
        return max(int(pages or appended or 1), 1)

    # Page-based charging for document uploads (invoice/receipt): charge by
    # pages by default, but when a single page packs multiple invoices/receipts
    # the segmentation captures each, so the charge must follow whichever is
    # larger. Never charge less than the number of new captured docs, and never
    # less than 1 when at least one doc was appended.
    pages = input_page_count or payload.get("input_page_count")
    pages_or_0 = int(pages) if pages else 0
    return max(pages_or_0, appended, 1)


def format_credit_footer(*, credits_used: int, credits_remaining: int) -> str:
    return format_coin_footer(
        credits_used=credits_used, credits_remaining=credits_remaining
    )


def credit_footer_block(*, credits_used: int, credits_remaining: int) -> dict:
    return _coin_credit_footer_block(
        credits_used=credits_used, credits_remaining=credits_remaining
    )


def batch_credit_footer_block(*, credits_used: int, credits_remaining: int) -> dict:
    from ledgr_slack.credits_view import format_batch_credit_summary

    return {
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": format_batch_credit_summary(
                    credits_used=credits_used,
                    credits_remaining=credits_remaining,
                ),
            }
        ],
    }


def dedup_credit_footer_block() -> dict:
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": format_dedup_credit_line()}],
    }


def charge_delivery_credits(
    *,
    firm_id: str | None,
    channel_id: str,
    file_id: str | None,
    kind: str,
    payload: dict,
    append_result: dict,
    input_page_count: int | None = None,
) -> dict | None:
    """Deduct credits after a successful delivery; idempotent per Slack file."""

    if not firm_id or not file_id:
        return None

    units = delivery_charge_units(
        kind=kind,
        payload=payload,
        append_result=append_result,
        input_page_count=input_page_count,
    )
    if units <= 0:
        return None

    idem = delivery_idempotency_key(channel_id=channel_id, file_id=file_id)
    try:
        service = get_shared_credit_service()
        remaining = service.deduct(
            firm_id,
            amount=units,
            reason="delivery",
            idempotency_key=idem,
            channel_id=channel_id,
        )
        return {"credits_used": units, "credits_remaining": int(remaining)}
    except Exception:  # noqa: BLE001 — billing must not undo a successful delivery
        # Delivery already succeeded, so we do NOT raise. But on the durable
        # (Firestore) path a swallowed deduct silently loses a real charge —
        # log LOUD with the idempotency key so the charge is recoverable, and
        # the deduct can be safely replayed later (idempotent per ``idem``).
        logger.error(
            "CREDIT CHARGE LOST: delivery deduct failed firm=%s units=%s idem=%s "
            "— delivery kept, replay this idempotency key to recover the charge",
            firm_id,
            units,
            idem,
            exc_info=True,
        )
        return None
