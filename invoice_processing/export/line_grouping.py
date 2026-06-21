"""ERP-profile line grouping (M1) — verbatim by default, collapse only when YAML declares it.

Grouping rules live in ``erp_profiles/*.yaml`` under ``line_grouping``. Python implements
rule handlers; profiles choose which rules apply — no hardcoded telco lexicons in the
normalize path.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from ..extract.document_record import DocumentRecord
from ..extract.invoice_extractor import ExtractedLine

logger = logging.getLogger(__name__)

_GST_BUCKET_RE = re.compile(
    r"GST\s*@\s*(\d+(?:\.\d+)?)\s*%\s*on\s*\$?\s*([\d,]+\.?\d*)",
    re.I,
)

_DEFAULT_TELCO_MARKERS = (
    "telco",
    "mobile pte",
    "telecommunications",
    "broadband",
    "m1 ",
    "simba",
)


def _parse_amount(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,\-]", "", text.replace(",", ""))
    if not cleaned or cleaned in (".", "-", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def line_grouping_rules(profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return declared ``line_grouping`` rules from an ERP profile dict."""
    if not profile:
        return []
    raw = profile.get("line_grouping")
    if not raw:
        return []
    return list(raw)


def _norm_label(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").strip().lower()).rstrip(".")


def record_matches_telco_markers(
    record: DocumentRecord,
    markers: tuple[str, ...] = _DEFAULT_TELCO_MARKERS,
) -> bool:
    """True when capture text or GST bucket fields look like a telco/utility bill."""
    parts = [record.notes or ""]
    parts.extend(f"{f.label} {f.value}" for f in record.labeled_fields)
    parts.extend((line.description or "") for line in record.line_items[:8])
    blob = " ".join(parts).lower()
    if any(m in blob for m in markers):
        return True
    return any(
        _GST_BUCKET_RE.search(f.label or "") or _GST_BUCKET_RE.search(f.value or "")
        for f in record.labeled_fields
    )


def telco_gst_bucket_lines(record: DocumentRecord) -> Optional[list[ExtractedLine]]:
    """Build SR/ZR summary lines from GST @ rate% on $net labeled fields."""
    seen: set[tuple[float, float]] = set()
    buckets: list[tuple[float, float, float]] = []
    for f in record.labeled_fields:
        for text, gst_src in ((f.label, f.value), (f.value, f.label)):
            m = _GST_BUCKET_RE.search(text or "")
            if not m:
                continue
            rate = float(m.group(1))
            net = _parse_amount(m.group(2))
            if net is None:
                continue
            key = (rate, net)
            if key in seen:
                break
            seen.add(key)
            gst = _parse_amount(gst_src) or 0.0
            buckets.append((rate, net, gst))
            break

    if not buckets:
        return None

    lines: list[ExtractedLine] = []
    for rate, net, gst in buckets:
        if net == 0 and gst == 0:
            continue
        if rate == 0:
            lines.append(
                ExtractedLine(
                    description="Telecommunication services - zero rated",
                    net_amount=net,
                    gst_amount=0.0,
                    tax_label="ZR",
                )
            )
        else:
            lines.append(
                ExtractedLine(
                    description=f"Telecommunication services - standard rated ({rate:g}%)",
                    net_amount=net,
                    gst_amount=gst,
                    tax_label="SR",
                )
            )
    return lines or None


def _telco_current_charges(record: DocumentRecord) -> Optional[float]:
    for f in list(record.totals) + list(record.labeled_fields):
        nl = _norm_label(f.label)
        if "current charges" in nl or nl == "current charges":
            amt = _parse_amount(f.value)
            if amt is not None:
                return amt
    return None


def _apply_telco_gst_buckets(
    record: DocumentRecord,
    lines: list[ExtractedLine],
    rule: dict[str, Any],
) -> list[ExtractedLine]:
    markers = tuple(rule.get("issuer_markers") or _DEFAULT_TELCO_MARKERS)
    if not record_matches_telco_markers(record, markers=markers):
        return lines
    grouped = telco_gst_bucket_lines(record)
    if not grouped:
        return lines
    logger.info(
        "line_grouping telco_gst_buckets: collapsed %d capture lines to %d ledger lines",
        len(lines),
        len(grouped),
    )
    return grouped


_RULE_HANDLERS = {
    "telco_gst_buckets": _apply_telco_gst_buckets,
}


def apply_line_grouping_to_lines(
    record: DocumentRecord,
    lines: list[ExtractedLine],
    profile: dict[str, Any] | None,
) -> list[ExtractedLine]:
    """Apply ERP-declared grouping rules to verbatim capture lines (default: passthrough)."""
    out = list(lines)
    for rule in line_grouping_rules(profile):
        rule_id = (rule.get("rule") or "").strip()
        handler = _RULE_HANDLERS.get(rule_id)
        if handler is None:
            logger.warning("unknown line_grouping rule %r — skipped", rule_id)
            continue
        out = handler(record, out, rule)
    return out


def telco_grouped_totals(record: DocumentRecord, lines: list[ExtractedLine]) -> tuple[float, float, float]:
    """Recompute subtotal/gst/total after telco bucket grouping."""
    subtotal = round(sum(ln.net_amount or 0.0 for ln in lines), 2)
    gst_total = round(sum(ln.gst_amount or 0.0 for ln in lines), 2)
    total = _telco_current_charges(record) or round(subtotal + gst_total, 2)
    return subtotal, gst_total, total
