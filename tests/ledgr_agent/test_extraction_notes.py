"""Unit tests for read-only extraction note annotations."""

from __future__ import annotations

from ledgr_agent.internal.extraction_notes import annotate_over_extraction_notes


def test_annotate_notes_does_not_mutate_lines() -> None:
    lines = [{"description": f"Row {i}", "net_amount": 1.0} for i in range(12)]
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [{"line_grain": "summary", "lines": lines}],
    }
    result = annotate_over_extraction_notes(bundle)
    assert result["documents"][0]["lines"] == lines
    assert "notes" in result["documents"][0]


def test_annotate_skips_itemized_documents() -> None:
    lines = [
        {"description": "Widget", "quantity": 2, "unit_amount": 10.0, "net_amount": 20.0}
        for _ in range(12)
    ]
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [{"line_grain": "itemized", "lines": lines}],
    }
    result = annotate_over_extraction_notes(bundle)
    assert not result["documents"][0].get("notes")
