"""Firestore session state sanitization tests."""

from accounting_agents.firestore_safe import find_nested_arrays, firestore_safe_state
from invoice_processing.extract.document_normalizer import slim_document_record_for_state
from invoice_processing.extract.document_record import DocumentRecord, TableCapture


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


def test_slim_document_record_drops_tables_for_firestore():
    record = DocumentRecord(
        tables=[
            TableCapture(
                name="expenses",
                headers=["Desc", "Amt"],
                rows=[["Travel", "100"], ["Hotel", "200"]],
            )
        ],
        line_items=[],
    )
    slim = slim_document_record_for_state(record)
    assert slim["tables"] == []
