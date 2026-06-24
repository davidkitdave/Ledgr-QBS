from __future__ import annotations

from ledgr_agent.review.classifier import classify_review_reason
from ledgr_agent.schemas.review import ReviewRequest, SoftWarning


def partition_and_group_reasons(
    reasons: list[str],
    *,
    file_name: str | None = None,
) -> tuple[list[ReviewRequest], list[SoftWarning]]:
    """Split legacy reason strings into hard stops and grouped soft warnings."""

    hard: list[ReviewRequest] = []
    account_flags: list[str] = []
    other_soft: list[str] = []

    for reason in reasons:
        severity = classify_review_reason(reason)
        if severity == "hard_review":
            hard.append(
                ReviewRequest(
                    id=f"hard_{len(hard)}",
                    severity="hard_review",
                    message=reason,
                    file_name=file_name,
                )
            )
            continue
        if "flagged for account review" in reason.lower():
            account_flags.append(reason)
        else:
            other_soft.append(reason)

    soft: list[SoftWarning] = []
    if account_flags:
        soft.append(
            SoftWarning(
                id="low_coa_confidence_group",
                message=(
                    f"{len(account_flags)} lines have low-confidence account mapping. "
                    "Suggested account: review mapping before approve."
                ),
                count=len(account_flags),
                file_name=file_name,
                payload={"reasons": account_flags},
            )
        )
    for idx, reason in enumerate(other_soft):
        soft.append(
            SoftWarning(
                id=f"soft_{idx}",
                message=reason,
                file_name=file_name,
            )
        )
    return hard, soft
