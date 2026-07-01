"""Tests for per-document ledger dedupe identity (WS-5.4)."""

from ledgr_slack.ledger_doc_identity import ledger_doc_identity, ledger_row_signature


def test_ledger_doc_identity_includes_page_range_and_reference():
    key = ledger_doc_identity("Purchase", "INV-A", (1, 1))
    assert key == "Purchase:INV-A:1-1"


def test_ledger_doc_identity_distinct_pages_same_reference():
    k1 = ledger_doc_identity("Purchase", "INV-SAME", (1, 2))
    k2 = ledger_doc_identity("Purchase", "INV-SAME", (3, 4))
    assert k1 != k2


def test_ledger_doc_identity_stable_without_file_id():
    """Re-drop idempotency: same reference+pages → same key (no Slack file id)."""
    k_first_drop = ledger_doc_identity("Purchase", "INV-200", (1, 1))
    k_redrop = ledger_doc_identity("Purchase", "INV-200", (1, 1))
    assert k_first_drop == k_redrop == "Purchase:INV-200:1-1"


def test_ledger_doc_identity_fallback_index_when_reference_missing():
    assert ledger_doc_identity("Purchase", None, (2, 2), index=1) == "Purchase:i1:2-2"
    assert ledger_doc_identity("Purchase", "", None, index=0) == "Purchase:i0"


def test_ledger_row_signature_normalizes_int_float_and_blank():
    """openpyxl reads 500.0 back as int 500 and a blank cell as None — both
    must hash identically to the append-side float/'' or the purge misses."""
    append_side = ledger_row_signature("Sales", "10/09/2025", "", 500.0)
    clear_side = ledger_row_signature("Sales", "10/09/2025", None, 500)
    assert append_side == clear_side == "Sales:sig:10/09/2025||500.0"


def test_ledger_row_signature_distinguishes_amount_and_code():
    base = ledger_row_signature("Sales", "10/09/2025", "ACME", 500.0)
    assert base != ledger_row_signature("Sales", "10/09/2025", "ACME", 600.0)
    assert base != ledger_row_signature("Sales", "10/09/2025", "OTHER", 500.0)
    assert base != ledger_row_signature("Sales", "11/09/2025", "ACME", 500.0)
