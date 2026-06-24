from __future__ import annotations

from datetime import date
from typing import Any


def expected_standard_rate(policy: dict[str, Any], *, invoice_date: date) -> float | None:
    rows = policy.get("rates", {}).get("standard", [])
    for row in rows:
        start = date.fromisoformat(str(row["effective_from"]))
        end_raw = row.get("effective_to")
        end = date.fromisoformat(str(end_raw)) if end_raw else date.max
        if start <= invoice_date <= end:
            return float(row["rate"])
    return None


def validate_gst_registration_gate(
    policy: dict[str, Any],
    *,
    client_profile: dict[str, Any],
    extracted: dict[str, Any],
) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    registered = bool(client_profile.get(policy["registration"]["client_flag"]))
    gst_total = float(extracted.get("gst_total") or 0.0)
    direction = str(extracted.get("direction_for_client") or "")
    if not registered and gst_total > 0 and direction == "purchase":
        violations.append(
            {
                "id": "gst_claimed_by_non_registered_client",
                "severity": "hard_review",
            }
        )
    return violations
