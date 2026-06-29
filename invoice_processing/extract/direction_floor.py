"""Deterministic direction floor from client entity_memory vendor roles (ADR-0027)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from ..export.client_context import EntityMemoryEntry

MatchKind = Literal["reg_no", "name_only"]


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
) -> Optional[tuple[str, MatchKind]]:
    """Return remembered buy/sell role and how it was matched, if any.

    ``reg_no`` matches (both sides non-empty and normalized equal) are trusted.
    ``name_only`` matches are weaker: invoice vendor names are untrusted text.
    """
    n_vendor = _norm(vendor_name)
    n_reg = _norm(vendor_reg_no)

    name_only_match: Optional[tuple[str, MatchKind]] = None

    for entry in entity_memory:
        role_direction = _role_to_direction(entry.role)
        if not role_direction:
            continue

        n_entry_name = _norm(entry.name)
        reg_hit = bool(n_reg) and bool(_norm(entry.reg_no)) and _norm(entry.reg_no) == n_reg
        name_hit = bool(n_vendor) and bool(n_entry_name) and n_entry_name == n_vendor

        if reg_hit:
            return entry.role, "reg_no"
        if name_hit and name_only_match is None:
            name_only_match = (entry.role, "name_only")

    return name_only_match


@dataclass(frozen=True)
class DirectionFloorResult:
    effective_direction: str
    needs_review: bool
    conflict: bool = False
    review_note: Optional[str] = None
    match_kind: Optional[MatchKind] = None


def apply_direction_floor(
    llm_direction: str,
    *,
    vendor_name: Optional[str],
    vendor_reg_no: Optional[str],
    entity_memory: list[EntityMemoryEntry],
) -> DirectionFloorResult:
    """Apply the client's remembered vendor role as a deterministic direction floor.

    - LLM ``unknown`` + role matched by ``reg_no`` → take role direction (no review).
    - LLM ``unknown`` + role matched by name only → pure LLM (needs review); names on
      invoices are untrusted and must not auto-clear HITL.
    - LLM agrees with role → proceed (name-only match is acceptable here).
    - LLM confidently disagrees with role → conflict review.
    - No role on file → pure LLM read (unknown still needs review).
    """
    llm = (llm_direction or "unknown").strip().lower()
    if llm == "self_referential":
        return DirectionFloorResult(
            effective_direction="self_referential",
            needs_review=True,
        )

    lookup = _lookup_vendor_role(vendor_name, vendor_reg_no, entity_memory)
    role = lookup[0] if lookup else None
    match_kind = lookup[1] if lookup else None
    role_direction = _role_to_direction(role)

    if not role_direction:
        needs_review = llm in ("unknown", "auto", "") or llm not in ("purchase", "sales")
        return DirectionFloorResult(
            effective_direction=llm if llm in ("purchase", "sales", "unknown") else "unknown",
            needs_review=needs_review,
            match_kind=match_kind,
        )

    if llm in ("unknown", "auto", ""):
        # Name-only matches must not silently bypass HITL when the LLM is unsure.
        if match_kind == "name_only":
            return DirectionFloorResult(
                effective_direction="unknown",
                needs_review=True,
                match_kind=match_kind,
            )
        return DirectionFloorResult(
            effective_direction=role_direction,
            needs_review=False,
            match_kind=match_kind,
        )

    if llm in ("purchase", "sales") and llm == role_direction:
        return DirectionFloorResult(
            effective_direction=llm,
            needs_review=False,
            match_kind=match_kind,
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
            match_kind=match_kind,
        )

    return DirectionFloorResult(
        effective_direction="unknown",
        needs_review=True,
        match_kind=match_kind,
    )
