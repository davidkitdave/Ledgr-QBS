"""Tournament metrics for agents-cli eval compare (document_record_field_recall)."""

from __future__ import annotations

from typing import Any


def document_record_completeness(result_row: dict[str, Any]) -> float:
    """Extract completeness score from a tournament result row."""
    return float(result_row.get("completeness") or 0.0)


def tournament_aggregate_score(report: dict[str, Any]) -> float:
    """Mean score across all variant×fixture rows in a tournament report."""
    rows = report.get("results") or []
    if not rows:
        return 0.0
    return sum(float(r.get("score") or 0.0) for r in rows) / len(rows)
