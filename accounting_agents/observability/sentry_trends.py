"""WS-6.4 — Sentry trend events for pipeline quality signals.

Emits structured info-level messages (not errors) when the pipeline self-detects
reconcile failures or account-code flags. All calls are no-ops when ``SENTRY_DSN``
is unset so unit tests and offline dev stay hermetic.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import sentry_sdk

from accounting_agents.normalized_invoice_codec import dict_to_invoice
from invoice_processing.export.models import NormalizedInvoice

_CLASSIFY_CONFIDENCE_KEY = "classify_confidence"
_DIRECTION_KEY = "direction"
_NORMALIZED_KEY = "normalized_invoices"

# Machine-reason prefixes worth trending once WS-1.5 flags exist.
_TREND_REASON_PREFIXES = (
    "unreconciled:",
    "blank_account_code:",
    "account_code_not_in_coa:",
)

_initialized = False


def init_sentry_if_configured() -> None:
    """Lazy-init Sentry when ``SENTRY_DSN`` is set; otherwise no-op."""
    global _initialized
    if _initialized:
        return
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        _initialized = True
        return
    environment = os.environ.get("SENTRY_ENVIRONMENT", "").strip() or None
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        traces_sample_rate=0.0,
    )
    _initialized = True


def _sentry_enabled() -> bool:
    return bool(os.environ.get("SENTRY_DSN", "").strip())


def emit_pipeline_quality_event(
    *,
    client_id: str,
    vendor: Optional[str],
    reconciled: bool,
    reason: str,
    confidence: Optional[float] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Emit one info-level Sentry trend event; no-op without ``SENTRY_DSN``."""
    if not _sentry_enabled():
        return
    init_sentry_if_configured()

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("client_id", client_id or "unknown")
        scope.set_tag("vendor", vendor or "unknown")
        scope.set_tag("reconciled", "true" if reconciled else "false")
        scope.set_tag("reason", reason)
        if confidence is not None:
            scope.set_tag("confidence", str(confidence))
        if extra:
            for key, value in extra.items():
                scope.set_extra(key, value)
        sentry_sdk.capture_message("pipeline.quality", level="info")


def _invoices_from_state(state: dict) -> list[NormalizedInvoice]:
    return [dict_to_invoice(d) for d in (state.get(_NORMALIZED_KEY) or [])]


def _vendor_for_invoice(inv: NormalizedInvoice, state: dict) -> Optional[str]:
    doc_dir = (state.get(_DIRECTION_KEY) or "purchase").strip().lower()
    if doc_dir == "purchase":
        party = inv.supplier
    else:
        party = inv.customer
    if party is None:
        return None
    return party.name


def _invoice_label(inv: NormalizedInvoice, idx: int) -> str:
    return inv.invoice_number or f"invoice #{idx + 1}"


def _reason_targets_label(reason: str, label: str) -> bool:
    needle = f": {label}"
    return needle in reason


def emit_from_struggle_state(state: dict, reasons: list[str]) -> None:
    """Emit trend events for reconcile / account-code struggle signals."""
    if not _sentry_enabled():
        return

    invoices = _invoices_from_state(state)
    has_unreconciled = any(not inv.reconciled for inv in invoices)
    trend_reasons = [
        r for r in reasons
        if any(r.startswith(prefix) for prefix in _TREND_REASON_PREFIXES)
    ]
    if not reasons and not has_unreconciled:
        return
    if not trend_reasons and not has_unreconciled:
        return

    client_id = state.get("client_id") or "unknown"
    confidence = state.get(_CLASSIFY_CONFIDENCE_KEY)
    emitted_reconcile: set[str] = set()

    for idx, inv in enumerate(invoices):
        if inv.reconciled:
            continue
        label = _invoice_label(inv, idx)
        if label in emitted_reconcile:
            continue
        emitted_reconcile.add(label)
        matching = [
            r for r in reasons
            if r.startswith("unreconciled:") and _reason_targets_label(r, label)
        ]
        reason = matching[0] if matching else "reconcile_failed"
        emit_pipeline_quality_event(
            client_id=client_id,
            vendor=_vendor_for_invoice(inv, state),
            reconciled=False,
            reason=reason,
            confidence=confidence,
            extra={"reconcile_note": inv.reconcile_note},
        )

    for reason in trend_reasons:
        if reason.startswith("unreconciled:"):
            continue
        vendor: Optional[str] = None
        reconciled = True
        for idx, inv in enumerate(invoices):
            label = _invoice_label(inv, idx)
            if _reason_targets_label(reason, label):
                vendor = _vendor_for_invoice(inv, state)
                reconciled = inv.reconciled
                break
        if vendor is None and invoices:
            vendor = _vendor_for_invoice(invoices[0], state)
        emit_pipeline_quality_event(
            client_id=client_id,
            vendor=vendor,
            reconciled=reconciled,
            reason=reason,
            confidence=confidence,
        )


def emit_account_flagged_from_state(state: dict) -> None:
    """Emit trend events for COA lines flagged after categorization (WS-3.3/3.4)."""
    if not _sentry_enabled():
        return

    client_id = state.get("client_id") or "unknown"
    confidence = state.get(_CLASSIFY_CONFIDENCE_KEY)

    for idx, inv in enumerate(_invoices_from_state(state)):
        vendor = _vendor_for_invoice(inv, state)
        label = _invoice_label(inv, idx)
        for ln_idx, line in enumerate(inv.lines):
            if not line.account_flagged:
                continue
            extra: dict[str, Any] = {
                "invoice_label": label,
                "line_index": ln_idx + 1,
            }
            if line.account_flag_reason:
                extra["account_flag_reason"] = line.account_flag_reason
            emit_pipeline_quality_event(
                client_id=client_id,
                vendor=vendor,
                reconciled=inv.reconciled,
                reason="account_flagged",
                confidence=confidence,
                extra=extra,
            )
