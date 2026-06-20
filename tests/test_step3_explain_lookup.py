"""Step 3 (C-1) — explain + lookup read tools for the chat assistant.

Pure function tests only — no live Slack, no live Gemini.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest


from accounting_agents.assistant import (
    LEDGER_DATA_KEY,
    _BASE_INSTRUCTION,
    assistant_agent,
    explain_categorization,
    explain_tax_treatment,
    list_recent_documents,
    lookup_row,
    summarize_recent_activity,
)
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from invoice_processing.export.tax_classifier import TaxClassifier


# ---------------------------------------------------------------------------
# Hermetic fixture: force the LLM call OFF for every test in this module so
# reason_one_invoice always uses the deterministic TaxClassifier fallback.
# This makes the explain_tax_treatment assertions stable in any environment
# (CI has no API key; local dev has a live key that produces natural-language
# reasons that don't contain the bare code substring).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _no_llm_call():
    """Patch _call_llm to return None, routing reason_one_invoice to _fallback_classify."""
    import unittest.mock as _mock

    with _mock.patch("accounting_agents.tax_reasoning._call_llm", return_value=None):
        yield


class _FakeToolContext:
    """Minimal stand-in for google.adk.tools.ToolContext (state dict only)."""

    def __init__(self, state: dict):
        self.state = state


def _ctx(**state) -> _FakeToolContext:
    return _FakeToolContext(state)


def _parse_json(raw: str) -> dict:
    return json.loads(raw)


# --------------------------------------------------------------------------- #
# explain_categorization
# --------------------------------------------------------------------------- #


def test_explain_categorization_entity_memory_hit():
    ctx = _ctx(
        entity_memory=[
            {"name": "Acme Professional Services", "mapping_code": "6100", "reg_no": "201912345A"},
        ],
        coa=[],
        category_mapping={},
    )
    raw = explain_categorization(ctx, "Acme Professional Services Pte Ltd", "Consulting fees")
    data = _parse_json(raw)
    assert data["source"] == "entity_memory"
    assert data["confidence"] == 0.95
    assert data["flagged"] is False
    assert data["account_code"] == "6100"


def test_explain_categorization_category_mapping_hit():
    ctx = _ctx(
        category_mapping={"Professional Fees": "6100"},
        coa=[],
        entity_memory=[],
    )
    raw = explain_categorization(
        ctx, "Unknown Vendor", "Monthly retainer", category="Professional Fees"
    )
    data = _parse_json(raw)
    assert data["source"] == "category_mapping"
    assert data["confidence"] == 0.9
    assert data["account_code"] == "6100"


def test_explain_categorization_coa_keyword_hit():
    ctx = _ctx(
        coa=[
            {
                "code": "6300",
                "description": "IT & Software",
                "keywords": "aws, cloud, hosting",
            }
        ],
        category_mapping={},
        entity_memory=[],
    )
    raw = explain_categorization(ctx, "Amazon Web Services", "AWS cloud hosting Jan")
    data = _parse_json(raw)
    assert data["source"] == "coa_keyword"
    assert data["confidence"] == 0.8
    assert data["account_code"] == "6300"


def test_explain_categorization_unresolved():
    ctx = _ctx(coa=[], category_mapping={}, entity_memory=[])
    raw = explain_categorization(ctx, "Mystery Vendor", "Unknown charge")
    data = _parse_json(raw)
    assert data["status"] == "unresolved"
    assert data["flagged"] is True
    assert not data.get("account_code")


# --------------------------------------------------------------------------- #
# explain_tax_treatment
# --------------------------------------------------------------------------- #


def test_explain_tax_treatment_sr_purchase_registered():
    ctx = _ctx(tax_registered=True, region="SINGAPORE", base_currency="SGD")
    raw = explain_tax_treatment(
        ctx,
        line_description="Office supplies",
        tax_keyword="SR",
        net_amount="100",
        gst_amount="9",
        doc_type="purchase",
        invoice_date="2025-06-01",
        our_gst_registered="true",
    )
    data = _parse_json(raw)
    assert data["tax_treatment"] == "SR"
    assert "SR" in data["tax_reason"]


def test_explain_tax_treatment_nt_master_gate():
    ctx = _ctx(tax_registered=False, region="SINGAPORE", base_currency="SGD")
    raw = explain_tax_treatment(
        ctx,
        line_description="Office supplies with GST shown",
        tax_keyword="SR",
        net_amount="100",
        gst_amount="9",
        doc_type="purchase",
        invoice_date="2025-06-01",
        our_gst_registered="false",
    )
    data = _parse_json(raw)
    assert data["tax_treatment"] == "NT"
    assert "not GST-registered" in data["tax_reason"]


def test_explain_tax_treatment_zr_zero_rated():
    ctx = _ctx(tax_registered=True, region="SINGAPORE", base_currency="SGD")
    raw = explain_tax_treatment(
        ctx,
        line_description="International freight",
        tax_keyword="ZR",
        net_amount="500",
        gst_amount="0",
        doc_type="purchase",
        invoice_date="2025-06-01",
        our_gst_registered="true",
    )
    data = _parse_json(raw)
    assert data["tax_treatment"] == "ZR"


def test_explain_tax_uses_canonical_field_names():
    ctx = _ctx(tax_registered=True, region="SINGAPORE", base_currency="SGD")
    clf = TaxClassifier()
    line = InvoiceLine(description="Telco IDD", tax_keyword="ZR", net_amount=50.0, gst_amount=0.0)
    inv = NormalizedInvoice(
        doc_type="purchase",
        invoice_date=date(2025, 6, 1),
        supplier=PartyInfo(name="Telco Provider B", country="SG"),
        our_gst_registered=True,
    )
    clf.classify_line(line, inv)
    expected = line.tax_treatment

    raw = explain_tax_treatment(
        ctx,
        line_description="Telco IDD",
        tax_keyword="ZR",
        net_amount="50",
        gst_amount="0",
        doc_type="purchase",
        invoice_date="2025-06-01",
        our_gst_registered="true",
    )
    data = _parse_json(raw)
    assert data["tax_treatment"] == expected


# --------------------------------------------------------------------------- #
# summarize_recent_activity
# --------------------------------------------------------------------------- #


def test_summarize_recent_activity_window():
    today = date.today()
    recent = (today - timedelta(days=5)).strftime("%d/%m/%Y")
    old = (today - timedelta(days=60)).strftime("%d/%m/%Y")
    rows = [
        {
            "Date": recent,
            "Account Code / COA": "6100-Software",
            "Source Amount": 100.0,
            "Doc Type": "P",
        },
        {
            "Date": recent,
            "Account Code / COA": "6200-Rent",
            "Source Amount": 200.0,
            "Doc Type": "P",
        },
        {
            "Date": old,
            "Account Code / COA": "6100-Software",
            "Source Amount": 999.0,
            "Doc Type": "P",
        },
    ]
    ctx = _ctx(**{LEDGER_DATA_KEY: rows})
    raw = summarize_recent_activity(ctx, days="30")
    data = _parse_json(raw)
    assert data["transaction_count"] == 2
    assert data["total_spend"] == 300.0
    assert data["by_category"]["6100-Software"] == 100.0
    assert data["by_category"]["6200-Rent"] == 200.0


def test_summarize_recent_activity_empty():
    ctx = _ctx()
    raw = summarize_recent_activity(ctx)
    assert "not loaded" in raw.lower()


# --------------------------------------------------------------------------- #
# lookup_row
# --------------------------------------------------------------------------- #


def test_lookup_row_finds_match():
    rows = [
        {
            "Description": "AWS cloud hosting",
            "Vendor": "Amazon Web Services",
            "Account Code / COA": "6300-IT",
            "Source Amount": 150.0,
            "Date": "01/06/2025",
            "Doc Type": "P",
            "Tax Rate": "SR",
            "_sheet": "Purchase",
        },
    ]
    ctx = _ctx(**{LEDGER_DATA_KEY: rows})
    raw = lookup_row(ctx, query="AWS")
    data = _parse_json(raw)
    assert len(data["matches"]) == 1
    assert data["matches"][0]["description"] == "AWS cloud hosting"


def test_lookup_row_no_match():
    ctx = _ctx(**{LEDGER_DATA_KEY: [{"Description": "Rent payment"}]})
    raw = lookup_row(ctx, query="zzznomatch")
    data = _parse_json(raw)
    assert data["matches"] == []


def test_lookup_row_respects_limit():
    rows = [
        {"Description": f"Item {i}", "Source Amount": float(i)} for i in range(5)
    ]
    ctx = _ctx(**{LEDGER_DATA_KEY: rows})
    raw = lookup_row(ctx, query="Item", limit="1")
    data = _parse_json(raw)
    assert len(data["matches"]) == 1


# --------------------------------------------------------------------------- #
# list_recent_documents
# --------------------------------------------------------------------------- #


def test_list_recent_documents_groups_rows():
    rows = [
        {
            "Date": "01/06/2025",
            "Source Filename": "invoice-a.pdf",
            "Doc Type": "P",
            "Source Amount": 100.0,
        },
        {
            "Date": "01/06/2025",
            "Source Filename": "invoice-a.pdf",
            "Doc Type": "P",
            "Source Amount": 50.0,
        },
        {
            "Date": "02/06/2025",
            "Source Filename": "invoice-b.pdf",
            "Doc Type": "P",
            "Source Amount": 200.0,
        },
    ]
    ctx = _ctx(**{LEDGER_DATA_KEY: rows})
    raw = list_recent_documents(ctx, limit="10")
    data = _parse_json(raw)
    assert len(data["documents"]) == 2
    by_file = {d["filename"]: d for d in data["documents"]}
    assert by_file["invoice-a.pdf"]["row_count"] == 2
    assert by_file["invoice-a.pdf"]["total"] == 150.0
    assert by_file["invoice-b.pdf"]["total"] == 200.0


def test_list_recent_documents_respects_limit():
    rows = [
        {
            "Date": f"0{i}/06/2025",
            "Source Filename": f"doc-{i}.pdf",
            "Doc Type": "P",
            "Source Amount": 10.0,
        }
        for i in range(1, 4)
    ]
    ctx = _ctx(**{LEDGER_DATA_KEY: rows})
    raw = list_recent_documents(ctx, limit="1")
    data = _parse_json(raw)
    assert len(data["documents"]) == 1


# --------------------------------------------------------------------------- #
# P0-2 regression: list_recent_documents + summarize_recent_activity must see
# bank-statement rows regardless of transaction date
# --------------------------------------------------------------------------- #

# Bank rows from a Sample Partner-style workbook (Dec 2025, uploaded 2026-06-16).
# They have Withdrawal/Deposit/Balance columns — _is_bank_row returns True.
_SAMPLE_PARTNER_BANK_ROWS = [
    {
        "Date": "01/12/2025",
        "Description": "Opening balance",
        "Withdrawal": None,
        "Deposit": None,
        "Balance": 5000.0,
        "Currency": "SGD",
        "Source Filename": "2025 12.pdf",
        "_sheet": "OCBC - 0001",
    },
    {
        "Date": "15/12/2025",
        "Description": "Vendor payment",
        "Withdrawal": 1200.0,
        "Deposit": None,
        "Balance": 3800.0,
        "Currency": "SGD",
        "Source Filename": "2025 12.pdf",
        "_sheet": "OCBC - 0001",
    },
    {
        "Date": "28/12/2025",
        "Description": "Interest credit",
        "Withdrawal": None,
        "Deposit": 12.50,
        "Balance": 3812.50,
        "Currency": "SGD",
        "Source Filename": "2025 12.pdf",
        "_sheet": "OCBC - 0001",
    },
    {
        "Date": "31/12/2025",
        "Description": "Closing balance",
        "Withdrawal": None,
        "Deposit": None,
        "Balance": 3812.50,
        "Currency": "SGD",
        "Source Filename": "2025 12.pdf",
        "_sheet": "OCBC - 0001",
    },
]


def test_list_recent_documents_includes_bank_statement_rows():
    """list_recent_documents must surface bank-statement source docs.

    Reproduces P0-2: the Sample Partner Dec 2025 bank statement was uploaded
    2026-06-16 but list_recent_documents returned empty because every row
    has Withdrawal/Deposit/Balance columns and _is_bank_row skipped them all.
    The tool must group bank rows by (Date, Source Filename) and include them
    so the user can see 'what documents have been processed in this channel?'
    """
    ctx = _ctx(**{LEDGER_DATA_KEY: _SAMPLE_PARTNER_BANK_ROWS})
    raw = list_recent_documents(ctx, limit="10")
    data = _parse_json(raw)
    assert len(data["documents"]) >= 1, (
        "list_recent_documents returned no documents even though 4 bank rows "
        "from '2025 12.pdf' are loaded — bank-statement docs must appear"
    )
    filenames = {d["filename"] for d in data["documents"]}
    assert "2025 12.pdf" in filenames, (
        f"Expected '2025 12.pdf' in documents, got {filenames!r}"
    )


def test_summarize_recent_activity_names_fy_when_only_old_bank_rows():
    """summarize_recent_activity must name the available data when result is empty.

    Reproduces P0-2: Dec 2025 bank rows are > 30 days old (today is 2026-06-16).
    The 30-day window returns nothing, and the existing 'newest_hint' path only
    inspects INVOICE rows — bank rows are silently skipped in that pass too,
    so the user gets 'No transactions found in the last 30 days.' with no hint.
    The response must name either the most-recent bank date OR the FY so the
    user knows the data IS there and can ask for a wider view.
    """
    ctx = _ctx(**{LEDGER_DATA_KEY: _SAMPLE_PARTNER_BANK_ROWS})
    raw = summarize_recent_activity(ctx, days="30")
    # Must NOT just say "no transactions" with zero guidance
    assert "2025" in raw or "Dec" in raw.lower() or "december" in raw.lower(), (
        f"Expected the response to name the available bank data period, got: {raw!r}"
    )


# --------------------------------------------------------------------------- #
# Agent registration + prompt
# --------------------------------------------------------------------------- #


def test_assistant_agent_has_twenty_four_tools():
    # Step 3 added the 12 read tools; Step 4 (ADR-0009) adds the two gated
    # write tools (amend_ledger_row / remove_ledger_row) → 14; Step 7 adds the
    # direct learn_mapping tool → 15; Step 7/C-3 adds the gated
    # replace_recorded_month tool → 16; Step 7/ADR-0010 adds the gated
    # re_extract_document tool → 17; chat introspection adds
    # explain_document_processing → 18. P1 (2026-06-16) adds the four
    # diagnostic / introspection tools
    # (diagnose_assistant_context, list_processing_history,
    # get_document_processing_detail, list_pending_reviews) → 22;
    # explain_posted_line → 23; learn_mapping counted with gated writes → 24.
    assert assistant_agent.mode is None
    assert len(assistant_agent.tools) == 24


def test_assistant_instruction_mentions_new_tools():
    """P5-slim: instruction carries routing bullets, not a full tool catalog."""
    for name in (
        "diagnose_assistant_context",
        "get_document_processing_detail",
        "list_processing_history",
        "list_pending_reviews",
        "list_recent_documents",
        "lookup_row",
    ):
        assert name in _BASE_INSTRUCTION
    assert "explain_categorization" in _BASE_INSTRUCTION
    assert "lookup_row" in _BASE_INSTRUCTION
