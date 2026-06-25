from __future__ import annotations

import re

from ledgr_agent.review.classifier import (
    _is_missing_fields_review,
    _is_reconcile_mismatch,
    classify_review_reason,
)
from ledgr_agent.schemas.review import ReviewRequest, SoftWarning

_GROUPABLE_SOFT_IDS = frozenset(
    {"low_coa_confidence_group", "reconcile_mismatch_group", "missing_fields_group"}
)

_MISSING_FIELDS_RE = re.compile(
    r"needs review:\s*missing\s+(.+)$|^\s*missing\s+(.+)$",
    re.IGNORECASE,
)


def _missing_fields_label(reasons: list[str]) -> str:
    fields: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        match = _MISSING_FIELDS_RE.search(reason.strip())
        if match:
            field_text = (match.group(1) or match.group(2) or "").strip()
            for field in field_text.split(","):
                name = field.strip()
                if name and name not in seen:
                    seen.add(name)
                    fields.append(name)
    if fields:
        return ", ".join(fields)
    return "required fields"


def partition_and_group_reasons(
    reasons: list[str],
    *,
    file_name: str | None = None,
) -> tuple[list[ReviewRequest], list[SoftWarning]]:
    """Split legacy reason strings into hard stops and grouped soft warnings."""

    hard: list[ReviewRequest] = []
    account_flags: list[str] = []
    reconcile_flags: list[str] = []
    missing_field_flags: list[str] = []
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
        elif _is_reconcile_mismatch(reason):
            reconcile_flags.append(reason)
        elif _is_missing_fields_review(reason):
            missing_field_flags.append(reason)
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
    if reconcile_flags:
        soft.append(
            SoftWarning(
                id="reconcile_mismatch_group",
                message=(
                    f"{len(reconcile_flags)} line/total mismatches — review before posting."
                ),
                count=len(reconcile_flags),
                file_name=file_name,
                payload={"reasons": reconcile_flags},
            )
        )
    if missing_field_flags:
        fields = _missing_fields_label(missing_field_flags)
        soft.append(
            SoftWarning(
                id="missing_fields_group",
                message=f"Missing required fields ({fields}) — review before posting.",
                count=len(missing_field_flags),
                file_name=file_name,
                payload={"reasons": missing_field_flags},
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


def merge_soft_warnings(warnings: list[SoftWarning]) -> list[SoftWarning]:
    """Merge grouped soft warnings across sub-documents and dedupe reason strings."""

    merged: list[SoftWarning] = []
    buckets: dict[str, list[SoftWarning]] = {}

    for warning in warnings:
        if warning.id in _GROUPABLE_SOFT_IDS:
            buckets.setdefault(warning.id, []).append(warning)
        else:
            merged.append(warning)

    for group_id, items in buckets.items():
        doc_count = len(items)
        deduped_reasons: list[str] = []
        seen: set[str] = set()
        for item in items:
            for reason in item.payload.get("reasons") or []:
                text = str(reason)
                if text not in seen:
                    seen.add(text)
                    deduped_reasons.append(text)

        if group_id == "reconcile_mismatch_group":
            doc_word = "document" if doc_count == 1 else "documents"
            message = (
                f"{doc_count} {doc_word} have line/total mismatches — review before posting."
            )
        elif group_id == "missing_fields_group":
            fields = _missing_fields_label(deduped_reasons)
            doc_word = "document" if doc_count == 1 else "documents"
            message = (
                f"{doc_count} {doc_word} missing required fields ({fields}) — "
                "review before posting."
            )
        elif group_id == "low_coa_confidence_group":
            line_count = sum(item.count for item in items)
            message = (
                f"{line_count} lines have low-confidence account mapping. "
                "Suggested account: review mapping before approve."
            )
            doc_count = line_count
        else:
            message = items[0].message

        merged.append(
            SoftWarning(
                id=group_id,
                message=message,
                count=doc_count,
                payload={"reasons": deduped_reasons},
            )
        )

    return merged
