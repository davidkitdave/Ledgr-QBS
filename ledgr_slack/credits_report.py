"""Read credit balances and usage for Slack visibility surfaces."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ledgr_agent.billing import _ns, get_shared_credit_service

_CREDIT_FIRMS_COLLECTION = "credit_firms"
_DEDUCTS_SUBCOLLECTION = "deducts"


def read_firm_balance(firm_id: str) -> int:
    return int(get_shared_credit_service().read_balance(firm_id))


def _deducts_collection(firm_id: str) -> Any:
    from google.cloud import firestore

    db = firestore.Client()
    collection = _ns(_CREDIT_FIRMS_COLLECTION)
    return (
        db.collection(collection)
        .document(str(firm_id))
        .collection(_DEDUCTS_SUBCOLLECTION)
    )


def usage_by_channel(firm_id: str, *, month: str | None = None) -> dict[str, int]:
    """Sum deducted credits per channel from Firestore deduct markers."""

    month_prefix = month or datetime.now(timezone.utc).strftime("%Y-%m")
    totals: dict[str, int] = {}
    try:
        for snap in _deducts_collection(firm_id).stream():
            data = snap.to_dict() or {}
            channel_id = str(data.get("channel_id") or "").strip()
            if not channel_id:
                continue
            at = str(data.get("at") or "")
            if at and not at.startswith(month_prefix):
                continue
            try:
                amount = int(data.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0
            if amount > 0:
                totals[channel_id] = totals.get(channel_id, 0) + amount
    except Exception:
        return {}
    return totals


def channel_usage(firm_id: str, channel_id: str) -> int:
    return usage_by_channel(firm_id).get(channel_id, 0)
