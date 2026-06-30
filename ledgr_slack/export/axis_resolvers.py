"""Backward-compatible shim — ``resolve_software`` inlined in ``ledgr_slack.ledger_store_base``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Optional, TypeVar

from ledgr_slack.export.exporters import normalize_software_key
from ledgr_slack.export.tax_classifier import TaxClassifier, get_tax_classifier

T = TypeVar("T")


@dataclass(frozen=True)
class AxisResolution(Generic[T]):
    value: T
    flagged: bool = False
    reason: str = ""


def resolve_software(raw: Optional[str]) -> AxisResolution[Optional[str]]:
    key = normalize_software_key(raw)
    if key is not None:
        return AxisResolution(key)
    if raw and str(raw).strip():
        return AxisResolution(None, flagged=True, reason=f"unknown software: {raw!r}")
    return AxisResolution(None, flagged=True, reason="software not set")


def resolve_currency(
    raw: Optional[str],
    *,
    client_region: str = "",
    client_currency: str = "",
) -> AxisResolution[str]:
    if raw and str(raw).strip():
        return AxisResolution(str(raw).strip().upper())
    if client_currency and str(client_currency).strip():
        return AxisResolution(str(client_currency).strip().upper())
    if client_region:
        from ledgr_slack.jurisdiction import REGION_REGISTRY, _norm_region

        entry = REGION_REGISTRY.get(_norm_region(client_region))
        if entry:
            return AxisResolution(entry["currency"])
    return AxisResolution("", flagged=True, reason="currency not on document and no client profile")


def resolve_tax_classifier_reference(
    reference: Optional[str],
    *,
    client_region: str = "",
) -> AxisResolution[Optional[TaxClassifier]]:
    ref = (reference or "").strip()
    if not ref and client_region:
        from ledgr_slack.jurisdiction import REGION_REGISTRY, _norm_region

        entry = REGION_REGISTRY.get(_norm_region(client_region))
        if entry:
            ref = entry["yaml"]
    if not ref:
        return AxisResolution(None, flagged=True, reason="jurisdiction_unresolved: no reference_yaml")
    if ref.upper() in ("AMBIGUOUS", "CROSS_BORDER"):
        return AxisResolution(None, flagged=True, reason=f"jurisdiction_unresolved: {ref}")
    clf = get_tax_classifier(ref)
    if clf is None:
        return AxisResolution(
            None,
            flagged=True,
            reason=f"jurisdiction_unresolved: unrecognised reference {ref!r}",
        )
    return AxisResolution(clf)
