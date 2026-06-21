"""ERP-profile line grouping (M1) — verbatim by default.

Grouping rules live in ``erp_profiles/*.yaml`` under ``line_grouping``. Python implements
rule handlers; profiles choose which rules apply.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..extract.document_record import DocumentRecord
from ..extract.invoice_extractor import ExtractedLine

logger = logging.getLogger(__name__)

RuleHandler = Callable[[DocumentRecord, list[ExtractedLine], dict[str, Any]], list[ExtractedLine]]


def line_grouping_rules(profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return declared ``line_grouping`` rules from an ERP profile dict."""
    if not profile:
        return []
    raw = profile.get("line_grouping")
    if not raw:
        return []
    return list(raw)


_RULE_HANDLERS: dict[str, RuleHandler] = {}


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
