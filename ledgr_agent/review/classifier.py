from __future__ import annotations

from ledgr_agent.schemas.review import ReviewSeverity

_HARD_MARKERS = (
    "not reconciled",
    "currency_mismatch",
    "jurisdiction",
    "tax region not set",
    "export cannot",
    "missing invoice",
    "direction unknown",
)

_SOFT_MARKERS = (
    "flagged for account review",
    "low tax confidence",
    "alternative coa",
)

_RECONCILE_MISMATCH_PREFIXES = ("subtotal:", "gst:", "total:")


def _is_reconcile_mismatch(reason: str) -> bool:
    lowered = reason.lower().strip()
    return any(lowered.startswith(prefix) for prefix in _RECONCILE_MISMATCH_PREFIXES) and " vs doc=" in lowered


def _is_missing_fields_review(reason: str) -> bool:
    lowered = reason.lower()
    return "needs review: missing " in lowered or lowered.startswith("missing ")


def classify_review_reason(reason: str) -> ReviewSeverity:
    """Map a legacy nodes.py reason string to hard_review or review."""

    lowered = reason.lower()
    if any(marker in lowered for marker in _HARD_MARKERS):
        return "hard_review"
    if any(marker in lowered for marker in _SOFT_MARKERS):
        return "review"
    if _is_reconcile_mismatch(reason) or _is_missing_fields_review(reason):
        return "review"
    return "hard_review"
