"""Hermetic tests for the light YAML-driven ERP projector."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ledgr_agent.internal import export as erp_projection
from ledgr_agent.internal.export import DEFAULT_SYSTEMS, project
from ledgr_agent.tools.build_sheets import build_sheets


PURCHASE_DOC = {
    "doc_type": "purchase",
    "document_kind": "invoice",
    "vendor_name": "Acme Supplies Pte Ltd",
    "customer_name": "",
    "entity_tax_id": "201234567M",
    "invoice_number": "INV-1001",
    "invoice_date": "2026-01-15",
    "due_date": "2026-02-14",
    "currency": "SGD",
    "fx_rate": None,
    "subtotal": 100.0,
    "tax_total": 9.0,
    "grand_total": 109.0,
    "lines": [
        {
            "description": "Office paper",
            "quantity": 2,
            "unit_amount": 50.0,
            "net_amount": 100.0,
            "tax_amount": 9.0,
            "total_amount": 109.0,
        },
    ],
    "notes": "",
}

CREDIT_NOTE_DOC = {
    **PURCHASE_DOC,
    "document_kind": "other",
    "invoice_number": "CN-2001",
}


def test_module_has_no_invoice_processing_imports() -> None:
    root = Path(__file__).resolve().parents[2]
    for rel in (
        "ledgr_agent/internal/export.py",
        "ledgr_agent/tools/build_sheets.py",
    ):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "invoice_processing" not in alias.name
                    assert "accounting_agents" not in alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert "invoice_processing" not in node.module
                assert "accounting_agents" not in node.module


def test_default_systems_lists_all_four_erps() -> None:
    assert DEFAULT_SYSTEMS == ["qbs", "xero", "autocount", "sql_account"]


def test_project_all_four_erps_purchase_invoice() -> None:
    out = project(PURCHASE_DOC)
    assert out["systems"] == DEFAULT_SYSTEMS
    assert set(out["results"]) == set(DEFAULT_SYSTEMS)

    qbs = out["results"]["qbs"]
    assert qbs["software_name"] == "QBS Ledger"
    assert qbs["sheet"] == "Purchase"
    assert len(qbs["rows"]) == 1
    row = qbs["rows"][0]
    assert row["Vendor Name"] == "Acme Supplies Pte Ltd"
    assert row["Invoice Number"] == "INV-1001"
    assert row["Sub Total"] == 100.0
    assert row["Tax Amount"] == 9.0
    assert row["Total Amount"] == 109.0
    assert row["Account Code / COA"] == ""

    autocount = out["results"]["autocount"]
    assert autocount["sheet"] == "Purchase"
    ac_row = autocount["rows"][0]
    assert ac_row["DocNo"] == "<<New>>"
    assert ac_row["JournalType"] == "PURCHASE"
    assert ac_row["InclusiveTax"] == "F"
    assert ac_row["CreditorCode"] == ""
    assert ac_row["AccNo"] == ""
    assert ac_row["Amount"] == 100.0

    xero = out["results"]["xero"]
    x_row = xero["rows"][0]
    assert x_row["*ContactName"] == "Acme Supplies Pte Ltd"
    assert x_row["*InvoiceNumber"] == "INV-1001"
    assert x_row["*Quantity"] == 2.0
    assert x_row["*UnitAmount"] == 50.0
    assert x_row["*AccountCode"] == ""
    assert x_row["*TaxType"] == ""

    sql = out["results"]["sql_account"]
    assert sql["sheet"] == "Purchase"
    sql_row = sql["rows"][0]
    assert sql_row["DOCNO(20)"] == "INV-1001"
    assert sql_row["CODE(10)"] == ""
    assert sql_row["_ACCOUNT(10)"] == ""
    assert sql_row["_TAX(10)"] == ""
    assert sql_row["_AMOUNT"] == 100.0


def test_other_document_kind_reads_amounts_as_printed() -> None:
    out = project(CREDIT_NOTE_DOC, systems=["qbs"])
    row = out["results"]["qbs"]["rows"][0]
    assert row["Sub Total"] == 100.0
    assert row["Tax Amount"] == 9.0
    assert row["Total Amount"] == 109.0


def test_project_populates_tax_code_from_tax_treatment() -> None:
    doc = {
        **PURCHASE_DOC,
        "lines": [
            {
                **PURCHASE_DOC["lines"][0],
                "tax_treatment": "Standard-Rated 9%",
            }
        ],
    }
    out = project(doc, systems=["qbs", "xero"])
    assert out["results"]["qbs"]["rows"][0]["Tax Treatment"] == "Standard-Rated 9%"
    assert out["results"]["xero"]["rows"][0]["*TaxType"] == "Standard-Rated 9%"


TAX_BUCKET_DOC = {
    "doc_type": "purchase",
    "document_kind": "invoice",
    "line_grain": "summary",
    "vendor_name": "Fictional Telco Pte Ltd",
    "invoice_number": "TEL-SRZR-001",
    "invoice_date": "2026-06-20",
    "currency": "SGD",
    "subtotal": 1200.0,
    "tax_total": 99.0,
    "grand_total": 1299.0,
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


def test_project_tax_bucket_rows_map_description_and_tax_fields() -> None:
    out = project(TAX_BUCKET_DOC, systems=["qbs"])
    rows = out["results"]["qbs"]["rows"]
    assert len(rows) == 2
    assert rows[0]["Description"] == "Telephone charges (SR)"
    assert rows[0]["Tax Treatment"] == "Standard-Rated 9%"
    assert rows[0]["Tax Amount"] == 99.0
    assert rows[1]["Description"] == "Telephone charges (ZR)"
    assert rows[1]["Tax Treatment"] == "Zero-Rated 0%"
    assert rows[1]["Tax Amount"] == 0.0


def test_build_sheets_soa_does_not_inflate_credit_units() -> None:
    """SOA pages are skipped for export and must not add to the credit estimate."""
    from types import SimpleNamespace

    from ledgr_agent.billing import CreditService, InMemoryCreditStore, configure_shared_credit_service
    from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY

    configure_shared_credit_service(CreditService(InMemoryCreditStore()))
    ctx = SimpleNamespace(
        state={
            "firm_id": "T_TEST",
            "channel_id": "C1",
            "file_id": "F1",
            "defer_slack_delivery": True,
            READ_DOC_STATE_KEY: {
                "file_kind": "commercial_documents",
                "source_path": "mixed.pdf",
                "page_count": 1,
                "documents": [
                    {
                        "document_kind": "statement_of_account",
                        "lines": [{"description": "IA-001", "net_amount": 100.0}],
                    },
                    PURCHASE_DOC,
                ],
            },
        }
    )
    result = build_sheets(ctx)
    assert result["status"] == "success"
    assert result["credits"]["credits_estimated"] == 1
    assert result["credits"]["credit_status"] == "estimated"


def test_build_sheets_skips_statement_of_account() -> None:
    from types import SimpleNamespace

    from ledgr_agent.billing import CreditService, InMemoryCreditStore, configure_shared_credit_service
    from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY

    configure_shared_credit_service(CreditService(InMemoryCreditStore()))
    soa_doc = {
        "document_kind": "statement_of_account",
        "vendor_name": "Fictional Supplies Sdn Bhd",
        "lines": [{"description": "IA-001", "net_amount": 100.0}],
    }
    ctx = SimpleNamespace(
        state={
            "firm_id": "T_TEST",
            READ_DOC_STATE_KEY: {
                "file_kind": "commercial_documents",
                "source_path": "soa.pdf",
                "documents": [soa_doc, PURCHASE_DOC],
            },
        }
    )
    result = build_sheets(ctx)
    assert result["status"] == "success"
    assert result["sheet_count"] == 4


def test_build_sheets_keeps_credit_note_postable() -> None:
    from types import SimpleNamespace

    from ledgr_agent.billing import CreditService, InMemoryCreditStore, configure_shared_credit_service
    from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY

    configure_shared_credit_service(CreditService(InMemoryCreditStore()))
    credit_doc = {
        "document_kind": "credit_note",
        "vendor_name": "Fictional Audio Sdn Bhd",
        "invoice_number": "CNA-01",
        "lines": [{"description": "Speaker C", "net_amount": 50.0, "total_amount": 50.0}],
    }
    ctx = SimpleNamespace(
        state={
            "firm_id": "T_TEST",
            READ_DOC_STATE_KEY: {
                "file_kind": "commercial_documents",
                "source_path": "credit.pdf",
                "documents": [credit_doc],
            },
        }
    )
    result = build_sheets(ctx)
    assert result["status"] == "success"
    assert result["sheet_count"] == 4


def test_build_sheets_rejects_soa_only() -> None:
    from types import SimpleNamespace

    from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY

    ctx = SimpleNamespace(
        state={
            READ_DOC_STATE_KEY: {
                "file_kind": "commercial_documents",
                "source_path": "soa_only.pdf",
                "documents": [
                    {
                        "document_kind": "statement_of_account",
                        "vendor_name": "Fictional Supplies Sdn Bhd",
                        "lines": [{"description": "IA-001", "net_amount": 100.0}],
                    }
                ],
            },
        }
    )
    result = build_sheets(ctx)
    assert result["status"] == "error"
    assert "statement of account" in result["message"]


def test_build_sheets_tool_success() -> None:
    from types import SimpleNamespace

    from ledgr_agent.billing import CreditService, InMemoryCreditStore, configure_shared_credit_service
    from ledgr_agent.tools.read_doc import READ_DOC_STATE_KEY

    configure_shared_credit_service(CreditService(InMemoryCreditStore()))
    ctx = SimpleNamespace(
        state={
            "firm_id": "T_TEST",
            READ_DOC_STATE_KEY: {
                "file_kind": "commercial_documents",
                "source_path": "bill.pdf",
                "documents": [PURCHASE_DOC],
            },
        }
    )
    result = build_sheets(ctx)
    assert result["status"] == "success"
    assert result["sheet_count"] == 4


def test_build_sheets_requires_read_doc() -> None:
    from types import SimpleNamespace

    assert build_sheets(SimpleNamespace(state={}))["status"] == "error"


def test_unknown_system_raises() -> None:
    with pytest.raises(erp_projection.ExportSkillError):
        project(PURCHASE_DOC, systems=["unknown_erp"])


AUDITAIR_LIKE_DOC = {
    "doc_type": "purchase",
    "document_kind": "invoice",
    "vendor_name": "Auditair International Pte Ltd",
    "customer_name": "Cast Unity Pte Ltd",
    "entity_tax_id": "",
    "invoice_number": "INV-11861",
    "invoice_date": "2026-05-14",
    "due_date": "2026-06-13",
    "currency": "SGD",
    "fx_rate": None,
    "subtotal": 2800.0,
    "tax_total": 252.0,
    "grand_total": 3052.0,
    "lines": [
        {
            "description": "Audit for ISO 9001",
            "quantity": 1.0,
            "unit_amount": 2800.0,
            "net_amount": None,
            "tax_amount": None,
            "total_amount": 3052.0,
        },
    ],
    "notes": "",
}


def test_partial_line_without_net_or_tax_uses_line_total_amount() -> None:
    """When net/tax are missing, per-line total_amount is used (not document grand_total)."""
    out = project(AUDITAIR_LIKE_DOC, systems=["qbs"])
    row = out["results"]["qbs"]["rows"][0]
    assert row["Sub Total"] == 0.0
    assert row["Tax Amount"] == 0.0
    assert row["Total Amount"] == 3052.0


def test_multi_line_total_amount_is_per_line_not_invoice_grand_total() -> None:
    doc = {
        **PURCHASE_DOC,
        "grand_total": 500.0,
        "lines": [
            {
                "description": "Line A",
                "net_amount": 100.0,
                "tax_amount": 9.0,
            },
            {
                "description": "Line B",
                "net_amount": 200.0,
                "tax_amount": 18.0,
            },
        ],
    }
    out = project(doc, systems=["qbs"])
    rows = out["results"]["qbs"]["rows"]
    assert [r["Total Amount"] for r in rows] == [109.0, 218.0]


def test_partial_line_does_not_infer_when_multiple_lines() -> None:
    """Tax fallback to header only fires for single-line bills."""
    doc = {
        **AUDITAIR_LIKE_DOC,
        "lines": [
            {**AUDITAIR_LIKE_DOC["lines"][0], "description": "Line A"},
            {**AUDITAIR_LIKE_DOC["lines"][0], "description": "Line B"},
        ],
    }
    out = project(doc, systems=["qbs"])
    for row in out["results"]["qbs"]["rows"]:
        assert row["Tax Amount"] == 0.0
