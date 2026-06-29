"""Tests for ADR-0027 vendor-role direction floor."""

from __future__ import annotations

from invoice_processing.export.client_context import EntityMemoryEntry
from invoice_processing.extract.direction_floor import apply_direction_floor


def _creditor_vendor() -> list[EntityMemoryEntry]:
    return [
        EntityMemoryEntry(
            name="NTUC FairPrice",
            reg_no="201234567A",
            role="Creditor",
            mapping_code="6100",
        )
    ]


def test_unknown_llm_direction_uses_creditor_role_without_review_when_reg_no_matches():
    """Taught vendor + LLM unknown + trusted reg_no match → purchase, no review."""
    result = apply_direction_floor(
        "unknown",
        vendor_name="NTUC FairPrice",
        vendor_reg_no="201234567A",
        entity_memory=_creditor_vendor(),
    )
    assert result.effective_direction == "purchase"
    assert result.needs_review is False
    assert result.conflict is False


def test_unknown_llm_direction_name_only_match_still_needs_review():
    """Spoofed vendor names must not auto-clear HITL when LLM is unknown."""
    result = apply_direction_floor(
        "unknown",
        vendor_name="NTUC FairPrice",
        vendor_reg_no=None,
        entity_memory=_creditor_vendor(),
    )
    assert result.effective_direction == "unknown"
    assert result.needs_review is True
    assert result.conflict is False
    assert result.match_kind == "name_only"


def test_unknown_llm_direction_uses_debtor_role_as_sales_when_reg_no_matches():
    memory = [
        EntityMemoryEntry(
            name="Big Customer Ltd",
            reg_no="53123456A",
            role="Debtor",
            mapping_code="4000",
        ),
    ]
    result = apply_direction_floor(
        "unknown",
        vendor_name="Big Customer Ltd",
        vendor_reg_no="53123456A",
        entity_memory=memory,
    )
    assert result.effective_direction == "sales"
    assert result.needs_review is False


def test_llm_agrees_with_role_proceeds_without_review():
    result = apply_direction_floor(
        "purchase",
        vendor_name="NTUC FairPrice",
        vendor_reg_no=None,
        entity_memory=_creditor_vendor(),
    )
    assert result.effective_direction == "purchase"
    assert result.needs_review is False


def test_llm_confidently_disagrees_with_role_raises_conflict_review():
    """LLM sales vs remembered Creditor (purchase) → conflict review."""
    result = apply_direction_floor(
        "sales",
        vendor_name="NTUC FairPrice",
        vendor_reg_no=None,
        entity_memory=_creditor_vendor(),
    )
    assert result.needs_review is True
    assert result.conflict is True
    assert "role" in (result.review_note or "").lower()
    assert "sales" in (result.review_note or "").lower()
    assert "purchase" in (result.review_note or "").lower()


def test_brand_new_vendor_without_role_still_needs_review():
    result = apply_direction_floor(
        "unknown",
        vendor_name="Brand New Shop",
        vendor_reg_no=None,
        entity_memory=[],
    )
    assert result.effective_direction == "unknown"
    assert result.needs_review is True
    assert result.conflict is False


def test_vendor_matched_by_reg_no_when_name_differs():
    memory = [
        EntityMemoryEntry(
            name="Acme Corp Pte Ltd",
            reg_no="201234567A",
            role="Creditor",
        ),
    ]
    result = apply_direction_floor(
        "unknown",
        vendor_name="ACME CORP",
        vendor_reg_no="201234567A",
        entity_memory=memory,
    )
    assert result.effective_direction == "purchase"
    assert result.needs_review is False


def test_no_role_on_entry_degrades_to_pure_llm_read():
    memory = [EntityMemoryEntry(name="NTUC FairPrice", mapping_code="6100")]
    result = apply_direction_floor(
        "unknown",
        vendor_name="NTUC FairPrice",
        vendor_reg_no=None,
        entity_memory=memory,
    )
    assert result.effective_direction == "unknown"
    assert result.needs_review is True
