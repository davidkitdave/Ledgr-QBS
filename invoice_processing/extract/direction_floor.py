"""Deterministic direction floor from client entity_memory vendor roles (ADR-0027)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..export.client_context import EntityMemoryEntry


def _norm(s: Optional[str]) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _role_to_direction(role: Optional[str]) -> Optional[str]:
    if not role:
        return None
    key = role.strip().lower()
    if key == "creditor":
        return "purchase"
    if key == "debtor":
        return "sales"
    return None


def _lookup_vendor_role(
    vendor_name: Optional[str],
    vendor_reg_no: Optional[str],
    entity_memory: list[EntityMemoryEntry],
) -> Optional[str]:
    """Return the remembered buy/sell role for a vendor, if any."""
    n_vendor = _norm(vendor_name)
    n_reg = _norm(vendor_reg_no)

    for entry in entity_memory:
        role_direction = _role_to_direction(entry.role)
        if not role_direction:
            continue

        n_entry_name = _norm(entry.name)
        reg_hit = bool(n_reg) and bool(_norm(entry.reg_no)) and _norm(entry.reg_no) == n_reg
        name_hit = bool(n_vendor) and bool(n_entry_name) and n_entry_name == n_vendor

        if reg_hit or name_hit:
            return entry.role

    return None


@dataclass(frozen=True)
class DirectionFloorResult:
    effective_direction: str
    needs_review: bool
    conflict: bool = False
    review_note: Optional[str] = None


def apply_direction_floor(
    llm_direction: str,
    *,
    vendor_name: Optional[str],
    vendor_reg_no: Optional[str],
    entity_memory: list[EntityMemoryEntry],
) -> DirectionFloorResult:
    """Apply the client's remembered vendor role as a deterministic direction floor.

    - LLM ``unknown`` + role present → take role direction (no review).
    - LLM agrees with role → proceed.
    - LLM confidently disagrees with role → conflict review.
    - No role on file → pure LLM read (unknown still needs review).
    """
    llm = (llm_direction or "unknown").strip().lower()
    if llm == "self_referential":
        return DirectionFloorResult(
            effective_direction="self_referential",
            needs_review=True,
        )

    role = _lookup_vendor_role(vendor_name, vendor_reg_no, entity_memory)
    role_direction = _role_to_direction(role)

    if not role_direction:
        needs_review = llm in ("unknown", "auto", "") or llm not in ("purchase", "sales")
        return DirectionFloorResult(
            effective_direction=llm if llm in ("purchase", "sales", "unknown") else "unknown",
            needs_review=needs_review,
        )

    if llm in ("unknown", "auto", ""):
        return DirectionFloorResult(
            effective_direction=role_direction,
            needs_review=False,
        )

    if llm in ("purchase", "sales") and llm == role_direction:
        return DirectionFloorResult(
            effective_direction=llm,
            needs_review=False,
        )

    if llm in ("purchase", "sales") and llm != role_direction:
        role_label = (role or "").strip() or "unknown"
        return DirectionFloorResult(
            effective_direction=llm,
            needs_review=True,
            conflict=True,
            review_note=(
                "needs review: direction conflicts with remembered vendor role "
                f"({role_label} → {role_direction}; LLM read {llm})"
            ),
        )

    return DirectionFloorResult(
        effective_direction="unknown",
        needs_review=True,
    )
