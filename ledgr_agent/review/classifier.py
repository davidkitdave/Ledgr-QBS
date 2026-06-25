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


def classify_review_reason(reason: str) -> ReviewSeverity:
    """Map a legacy nodes.py reason string to hard_review or review."""

    lowered = reason.lower()
    if any(marker in lowered for marker in _HARD_MARKERS):
        return "hard_review"
    if any(marker in lowered for marker in _SOFT_MARKERS):
        return "review"
    return "hard_review"
