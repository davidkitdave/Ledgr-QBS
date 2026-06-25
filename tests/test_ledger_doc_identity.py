"""Tests for per-document ledger dedupe identity (WS-5.4)."""

from accounting_agents.ledger_doc_identity import ledger_doc_identity


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
