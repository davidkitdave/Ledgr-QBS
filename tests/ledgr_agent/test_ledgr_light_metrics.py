"""Unit tests for reference-free self-consistency scorer (no creds)."""

from __future__ import annotations

from ledgr_agent.eval.ledgr_light_metrics import (
    _documents_from_workbook,
    score_bookable_granularity_on_extraction,
    score_classification_on_extraction,
    score_itemized_fidelity_on_extraction,
    score_self_consistency_on_extraction,
    score_tax_bucket_fidelity_on_extraction,
)


def test_self_consistency_passes_balanced_invoice() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [
            {
                "vendor_name": "Fictional Supplies Pte Ltd",
                "invoice_number": "INV-SG-001",
                "subtotal": 100.0,
                "tax_total": 9.0,
                "grand_total": 109.0,
                "lines": [{"description": "Office stationery", "net_amount": 100.0}],
            }
        ],
    }
    scored = score_self_consistency_on_extraction(bundle)
    assert scored["overall"] == 1.0


def test_self_consistency_fails_mismatched_totals() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [
            {
                "vendor_name": "Fictional Supplies Pte Ltd",
                "invoice_number": "INV-SG-001",
                "subtotal": 100.0,
                "tax_total": 9.0,
                "grand_total": 200.0,
                "lines": [{"description": "Office stationery", "net_amount": 100.0}],
            }
        ],
    }
    scored = score_self_consistency_on_extraction(bundle)
    assert scored["overall"] < 1.0


def test_self_consistency_empty_payload_scores_zero() -> None:
    assert score_self_consistency_on_extraction({})["overall"] == 0.0


def test_documents_from_workbook_maps_sheet_rows() -> None:
    workbook = {
        "status": "success",
        "file_kind": "commercial_documents",
        "sheets": [
            {
                "title": "Purchase",
                "rows": [
                    {
                        "Invoice Number": "INV-SG-001",
                        "Vendor Name": "Fictional Supplies Pte Ltd",
                        "Description": "Office stationery",
                        "Source Amount": 100.0,
                        "Sub Total": 100.0,
                        "Tax Amount": 9.0,
                        "Total Amount": 109.0,
                    }
                ],
            }
        ],
    }
    docs = _documents_from_workbook(workbook)
    assert len(docs) == 1
    scored = score_self_consistency_on_extraction(
        {"file_kind": "commercial_documents", "documents": docs}
    )
    assert scored["overall"] == 1.0


def test_self_consistency_checks_tax_breakdown() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [
            {
                "vendor_name": "Fictional Mixed Pte Ltd",
                "invoice_number": "INV-SG-SRZR-001",
                "subtotal": 150.0,
                "tax_total": 9.0,
                "grand_total": 159.0,
                "tax_breakdown": [
                    {
                        "tax_treatment": "Standard-Rated 9%",
                        "taxable_amount": 100.0,
                        "tax_amount": 9.0,
                    },
                    {
                        "tax_treatment": "Zero-Rated 0%",
                        "taxable_amount": 50.0,
                        "tax_amount": 0.0,
                    },
                ],
                "lines": [
                    {"description": "Consulting SR", "net_amount": 100.0},
                    {"description": "Export ZR", "net_amount": 50.0},
                ],
            }
        ],
    }
    scored = score_self_consistency_on_extraction(bundle)
    assert scored["overall"] == 1.0
    assert scored["doc0_breakdown_sum_tax"] == 1.0
    assert scored["doc0_breakdown_sum_taxable"] == 1.0


def test_classification_passes_invoice() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [{"document_kind": "invoice"}],
    }
    score = score_classification_on_extraction(
        bundle,
        expected_file_kind="commercial_documents",
        expected_document_kind="invoice",
    )
    assert score == 1.0


def test_classification_fails_wrong_kind() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [{"document_kind": "receipt"}],
    }
    score = score_classification_on_extraction(
        bundle,
        expected_file_kind="commercial_documents",
        expected_document_kind="invoice",
    )
    assert score == 0.0


def test_classification_passes_mixed_document_kinds() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [
            {"document_kind": "invoice"},
            {"document_kind": "credit_note"},
        ],
    }
    score = score_classification_on_extraction(
        bundle,
        expected_file_kind="commercial_documents",
        expected_document_kind=None,
        expected_document_kinds=["invoice", "credit_note"],
        expected_document_count=2,
    )
    assert score == 1.0


def test_classification_passes_bank_statement() -> None:
    bundle = {
        "file_kind": "bank_statement",
        "accounts": [{"bank_name": "OCBC"}],
    }
    score = score_classification_on_extraction(
        bundle,
        expected_file_kind="bank_statement",
        expected_document_kind=None,
    )
    assert score == 1.0


def test_classification_checks_document_count() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [
            {"document_kind": "invoice"},
            {"document_kind": "invoice"},
        ],
    }
    assert (
        score_classification_on_extraction(
            bundle,
            expected_file_kind="commercial_documents",
            expected_document_kind="invoice",
            expected_document_count=3,
        )
        == 0.0
    )
    assert (
        score_classification_on_extraction(
            bundle,
            expected_file_kind="commercial_documents",
            expected_document_kind="invoice",
            expected_document_count=2,
        )
        == 1.0
    )


def test_bookable_granularity_skips_when_not_hierarchy_case() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [{"lines": [{"description": f"Line {i}"} for i in range(50)]}],
    }
    assert (
        score_bookable_granularity_on_extraction(bundle, expect_hierarchy_scope=False) == 1.0
    )


def test_bookable_granularity_fails_over_extracted_hierarchy_bill() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [{"lines": [{"description": f"Detail {i}"} for i in range(20)]}],
    }
    assert (
        score_bookable_granularity_on_extraction(bundle, expect_hierarchy_scope=True) == 0.0
    )


def test_bookable_granularity_passes_summary_rows() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [
            {
                "lines": [
                    {"description": "Electricity", "net_amount": 450.0},
                    {"description": "Water", "net_amount": 120.0},
                    {"description": "Waste", "net_amount": 30.0},
                ]
            }
        ],
    }
    assert (
        score_bookable_granularity_on_extraction(
            bundle, expect_hierarchy_scope=True, max_bookable_lines=15
        )
        == 1.0
    )


def test_self_consistency_passes_standalone_soa() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [
            {
                "document_kind": "statement_of_account",
                "vendor_name": "Fictional Supplies Sdn Bhd",
                "invoice_number": "SOA-2026-001",
                "subtotal": 500.0,
                "grand_total": 500.0,
                "lines": [
                    {"description": "IA-001", "net_amount": 100.0},
                    {"description": "IA-002", "net_amount": 250.0},
                    {"description": "IA-003", "net_amount": 150.0},
                ],
            }
        ],
    }
    scored = score_self_consistency_on_extraction(bundle)
    assert scored["overall"] == 1.0


def test_classification_rejects_forbidden_document_kind() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [
            {"document_kind": "statement_of_account"},
            {"document_kind": "invoice"},
        ],
    }
    assert (
        score_classification_on_extraction(
            bundle,
            expected_file_kind="commercial_documents",
            expected_document_kind="invoice",
            expected_document_count=2,
            forbid_document_kinds=["statement_of_account"],
        )
        == 0.0
    )


def test_itemized_fidelity_fails_collapsed_invoice() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [{"lines": [{"description": "All items"}]}],
    }
    assert (
        score_itemized_fidelity_on_extraction(
            bundle, expect_itemized_lines=True, min_bookable_lines=4
        )
        == 0.0
    )


def test_itemized_fidelity_passes_multiline_invoice() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [
            {
                "lines": [
                    {"description": "A", "quantity": 1, "unit_amount": 10.0},
                    {"description": "B", "quantity": 2, "unit_amount": 15.0},
                    {"description": "C", "quantity": 1, "unit_amount": 25.0},
                    {"description": "D", "quantity": 4, "unit_amount": 5.0},
                ]
            }
        ],
    }
    assert (
        score_itemized_fidelity_on_extraction(
            bundle, expect_itemized_lines=True, min_bookable_lines=4
        )
        == 1.0
    )


def test_tax_bucket_fidelity_skips_when_not_tax_bucket_case() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [{"lines": [{"description": "Internet Services"}]}],
    }
    assert score_tax_bucket_fidelity_on_extraction(bundle, expect_tax_buckets=False) == 1.0


def test_tax_bucket_fidelity_passes_sr_zr_telco_buckets() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [
            {
                "subtotal": 1200.0,
                "tax_breakdown": [
                    {"tax_treatment": "Standard-Rated 9%", "taxable_amount": 1100.0, "tax_amount": 99.0},
                    {"tax_treatment": "Zero-Rated 0%", "taxable_amount": 100.0, "tax_amount": 0.0},
                ],
                "lines": [
                    {
                        "description": "Telephone charges (SR)",
                        "net_amount": 1100.0,
                        "tax_amount": 99.0,
                        "tax_treatment": "Standard-Rated 9%",
                    },
                    {
                        "description": "Telephone charges (ZR)",
                        "net_amount": 100.0,
                        "tax_amount": 0.0,
                        "tax_treatment": "Zero-Rated 0%",
                    },
                ],
            }
        ],
    }
    assert score_tax_bucket_fidelity_on_extraction(bundle, expect_tax_buckets=True) == 1.0


def test_tax_bucket_fidelity_fails_service_category_rows() -> None:
    bundle = {
        "file_kind": "commercial_documents",
        "documents": [
            {
                "subtotal": 1200.0,
                "tax_breakdown": [
                    {"tax_treatment": "Standard-Rated 9%", "taxable_amount": 1100.0, "tax_amount": 99.0},
                    {"tax_treatment": "Zero-Rated 0%", "taxable_amount": 100.0, "tax_amount": 0.0},
                ],
                "lines": [
                    {"description": "Internet Services", "net_amount": 800.0, "tax_amount": 0.0},
                    {"description": "Mobile Services", "net_amount": 400.0, "tax_amount": 0.0},
                ],
            }
        ],
    }
    assert score_tax_bucket_fidelity_on_extraction(bundle, expect_tax_buckets=True) == 0.0
