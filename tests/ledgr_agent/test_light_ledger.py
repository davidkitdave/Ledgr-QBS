"""Hermetic tests for the light-vs-factory spike helpers."""
from __future__ import annotations

from ledgr_agent.tools.light_ledger import (
    LedgerDoc,
    LedgerRow,
    LedgerRowBundle,
    light_to_normalized,
)
from scripts.spike_light_vs_factory import compare_paths, summarize_factory, summarize_light


def test_light_to_normalized_maps_rows():
    doc = LedgerDoc(
        doc_type="invoice",
        vendor="Acme Pte Ltd",
        reference="INV-001",
        date="2025-01-15",
        currency="SGD",
        subtotal=100.0,
        tax_total=9.0,
        grand_total=109.0,
        rows=[
            LedgerRow(
                description="Widget",
                net_amount=100.0,
                gst_amount=9.0,
                tax_treatment="SR",
                account_code="6-3000",
            )
        ],
    )
    inv = light_to_normalized(doc)
    assert inv.supplier.name == "Acme Pte Ltd"
    assert inv.invoice_number == "INV-001"
    assert len(inv.lines) == 1
    assert inv.lines[0].tax_treatment == "SR"
    assert inv.lines[0].account_code == "6-3000"
    assert inv.doc_total == 109.0


def test_compare_paths_flags_tax_mismatch():
    light = {
        "doc_count": 1,
        "row_count": 2,
        "tax_line_count": 2,
        "export_rows": [
            {"tax_treatment": "SR", "account_code": "6-3000"},
            {"tax_treatment": "ZR", "account_code": "6-3000"},
        ],
        "bundle": {"documents": [{"grand_total": 109.0}]},
        "gemini_call_count": 1,
        "elapsed_seconds": 9.0,
    }
    factory = {
        "posted_document_count": 12,
        "export_row_count": 24,
        "export_rows": [
            {"tax_treatment": "SR", "account_code": "6-3000"},
            {"tax_treatment": "SR", "account_code": "6-3000"},
        ],
        "elapsed_seconds": 210.0,
    }
    verdict = compare_paths(light, factory)
    assert verdict["light_beats_factory"] is True
    assert verdict["matched"] is False
    fields = {m["field"] for m in verdict["mismatches"]}
    assert "tax_treatment" in fields


def test_summarize_light_from_bundle():
    bundle = LedgerRowBundle(
        documents=[
            LedgerDoc(
                doc_type="receipt",
                grand_total=50.0,
                rows=[LedgerRow(description="Coffee", net_amount=50.0, tax_treatment="NT")],
            )
        ]
    )
    light = {
        "doc_count": len(bundle.documents),
        "row_count": 1,
        "tax_line_count": 0,
        "export_rows": [{"tax_treatment": "NT", "account_code": None}],
        "bundle": bundle.model_dump(),
        "gemini_call_count": 1,
        "elapsed_seconds": 5.0,
    }
    summary = summarize_light(light)
    assert summary["doc_count"] == 1
    assert summary["tax_treatments"] == ["NT"]
    assert summary["grand_total"] == 50.0


def test_summarize_factory_export_rows():
    factory = {
        "posted_document_count": 1,
        "export_row_count": 1,
        "export_rows": [
            {
                "tax_treatment": "SR",
                "account_code": "6-3000",
                "Total Amount": 109.0,
            }
        ],
        "elapsed_seconds": 30.0,
        "llm_call_count": 4,
    }
    summary = summarize_factory(factory)
    assert summary["tax_treatments"] == ["SR"]
    assert summary["account_codes"] == ["6-3000"]
    assert summary["grand_total"] == 109.0
