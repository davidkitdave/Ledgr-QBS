"""Firestore session state sanitization tests."""

from accounting_agents.firestore_safe import find_nested_arrays, firestore_safe_state


def test_find_nested_arrays_in_table_rows():
    state = {
        "document_records": [
            {"tables": [{"headers": ["A", "B"], "rows": [["1", "2"], ["3", "4"]]}]}
        ]
    }
    hits = find_nested_arrays(state)
    assert any("rows" in h for h in hits)


def test_firestore_safe_state_flattens_table_rows():
    state = {
        "document_records": [
            {"tables": [{"headers": ["A"], "rows": [["x"]]}]}
        ]
    }
    safe = firestore_safe_state(state)
    row = safe["document_records"][0]["tables"][0]["rows"][0]
    assert row == {"cells": ["x"]}
    assert find_nested_arrays(safe) == []
