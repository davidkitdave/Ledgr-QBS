"""Delivery-card notes for partial extraction failures (G5)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ledgr_slack.export.models import NormalizedInvoice

PARTIAL_FAILURE_PREFIX = "partial extraction"


def build_partial_failure_warnings(
    normalized: list[NormalizedInvoice],
    *,
    page_coverage_ok: bool,
    page_coverage_detail: str,
    input_page_count: int | None,
) -> list[str]:
    """Return human-readable G5 warnings for the delivery card."""
    warnings: list[str] = []
    if not normalized:
        if input_page_count and input_page_count > 0:
            warnings.append(
                f"{PARTIAL_FAILURE_PREFIX}: no documents extracted from "
                f"{input_page_count} page(s) — entire file needs review"
            )
        return warnings

    n_total = len(normalized)
    n_reconciled = sum(1 for inv in normalized if inv.reconciled)
    n_failed = n_total - n_reconciled

    if n_failed and n_reconciled:
        failed_refs = [
            inv.invoice_number or inv.supplier.name or f"doc-{i + 1}"
            for i, inv in enumerate(normalized)
            if not inv.reconciled
        ]
        ref_sample = ", ".join(failed_refs[:3])
        if len(failed_refs) > 3:
            ref_sample += f" (+{len(failed_refs) - 3} more)"
        warnings.append(
            f"{PARTIAL_FAILURE_PREFIX}: {n_reconciled} of {n_total} documents "
            f"reconciled; {n_failed} failed ({ref_sample}) — good rows still delivered"
        )
    elif n_failed == n_total and n_total > 0:
        warnings.append(
            f"{PARTIAL_FAILURE_PREFIX}: all {n_total} extracted document(s) "
            "failed reconcile — review before import"
        )

    if not page_coverage_ok and page_coverage_detail != "ok":
        detail = page_coverage_detail
        if "gaps" in detail:
            warnings.append(
                f"{PARTIAL_FAILURE_PREFIX}: {detail} — document(s) may be missing; "
                f"extracted {n_total} from {input_page_count or '?'} page(s)"
            )
        else:
            warnings.append(
                f"{PARTIAL_FAILURE_PREFIX}: segmentation uncertain ({detail}) — "
                f"extracted {n_total} from {input_page_count or '?'} page(s)"
            )

    return warnings


def format_partial_failure_note(warnings: list[str] | None) -> str:
    """Format G5 warnings for Slack delivery cards."""
    if not warnings:
        return ""
    return "⚠️ " + " · ".join(warnings)
