"""Credit billing matrix — page count vs multi-doc on one page (ADR-0016).

Rules (commercial invoice/receipt):
  - Default: 1 page, 1 document → 1 credit
  - 1 page, N documents (multi-receipt scan) → N credits
  - M pages, 1 document → M credits
  - Charge = max(page_count, appended_rows, 1) at Slack delivery

Rules (bank statement):
  - Charge = source PDF page count (gate and delivery align)
"""

from __future__ import annotations

import pytest

from ledgr_agent.billing import (
    CreditService,
    InMemoryCreditStore,
    billable_units,
    configure_shared_credit_service,
    estimate_units_from_bytes,
)
from ledgr_slack.credit_adapter import (
    charge_delivery_credits,
    credit_gate_for_bytes,
    delivery_charge_units,
    wire_shared_credit_service,
)


@pytest.fixture(autouse=True)
def _credit_svc():
    service = CreditService(InMemoryCreditStore())
    configure_shared_credit_service(service)
    wire_shared_credit_service()
    yield service


# ---------------------------------------------------------------------------
# billable_units (in-tool: read_doc → build_sheets)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "file_kind,page_count,document_count,expected",
    [
        ("commercial_documents", 1, 1, 1),
        ("commercial_documents", 1, 3, 3),
        ("commercial_documents", 3, 1, 3),
        ("commercial_documents", 2, 4, 4),
        ("bank_statement", 1, 1, 1),
        ("bank_statement", 5, 1, 5),
        ("bank_statement", 3, 10, 3),
    ],
    ids=[
        "commercial_1pg_1doc",
        "commercial_1pg_3docs_multi_receipt",
        "commercial_3pg_1doc",
        "commercial_2pg_4docs",
        "bank_1pg",
        "bank_5pg",
        "bank_3pg_ignores_doc_count",
    ],
)
def test_billable_units_matrix(file_kind, page_count, document_count, expected) -> None:
    assert (
        billable_units(
            file_kind=file_kind,
            page_count=page_count,
            document_count=document_count,
        )
        == expected
    )


# ---------------------------------------------------------------------------
# delivery_charge_units (Slack post-delivery)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kind,pages,appended,expected",
    [
        ("invoice", 1, 1, 1),
        ("invoice", 1, 3, 3),
        ("receipt", 1, 2, 2),
        ("invoice", 3, 1, 3),
        ("invoice", 1, 0, 0),
        ("bank", 3, 1, 3),
        ("bank", 5, 2, 5),
    ],
    ids=[
        "invoice_1pg_1row",
        "invoice_1pg_3rows_multi_receipt",
        "receipt_1pg_2rows",
        "invoice_3pg_1row",
        "invoice_deduped_zero",
        "bank_3pg",
        "bank_5pg",
    ],
)
def test_delivery_charge_units_matrix(kind, pages, appended, expected) -> None:
    append_result = (
        {"appended": 0, "all_deduped": True}
        if appended == 0 and expected == 0
        else {"appended": appended, "deduped": 0}
    )
    units = delivery_charge_units(
        kind=kind,
        payload={"input_page_count": pages},
        append_result=append_result,
        input_page_count=pages,
    )
    assert units == expected


def test_delivery_charge_multi_receipt_never_undercharges_pages() -> None:
    """3 receipts on 1 page must not bill as 1 credit when 3 rows were appended."""
    units = delivery_charge_units(
        kind="invoice",
        payload={"input_page_count": 1},
        append_result={"appended": 3, "deduped": 0},
        input_page_count=1,
    )
    assert units == 3


def test_delivery_charge_multi_page_never_undercharges_docs() -> None:
    """3-page single invoice must bill 3 even if only 1 ledger row appended."""
    units = delivery_charge_units(
        kind="invoice",
        payload={"input_page_count": 3},
        append_result={"appended": 1, "deduped": 0},
        input_page_count=3,
    )
    assert units == 3


# ---------------------------------------------------------------------------
# Upload gate (pre-LLM) — page estimate only
# ---------------------------------------------------------------------------

def test_upload_gate_uses_page_estimate_not_doc_count(
    _credit_svc: CreditService, monkeypatch
) -> None:
    """Gate before Gemini only knows page count — 1 page passes with balance 1."""

    monkeypatch.setattr(
        "ledgr_slack.credit_adapter.estimate_upload_pages", lambda _d, _f: 1
    )
    _credit_svc.grant("T1", 1, note="test")

    decision = credit_gate_for_bytes(
        firm_id="T1",
        data=b"%PDF",
        filename="multi.pdf",
    )
    assert decision["allowed"] is True
    assert decision["required_units"] == 1


def test_upload_gate_blocks_when_pages_exceed_balance(
    _credit_svc: CreditService, monkeypatch
) -> None:
    monkeypatch.setattr(
        "ledgr_slack.credit_adapter.estimate_upload_pages", lambda _d, _f: 5
    )
    _credit_svc.grant("T1", 2, note="test")

    decision = credit_gate_for_bytes(
        firm_id="T1",
        data=b"%PDF",
        filename="bank.pdf",
    )
    assert decision["allowed"] is False
    assert decision["reason"] == "insufficient_credit"


# ---------------------------------------------------------------------------
# End-to-end deduct amounts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pages,appended,expected_balance_after",
    [
        (1, 1, 9),
        (1, 3, 7),
        (3, 1, 7),
    ],
    ids=["one_credit", "three_multi_receipt", "three_page_invoice"],
)
def test_charge_delivery_deducts_correct_amount(
    _credit_svc: CreditService, pages, appended, expected_balance_after
) -> None:
    _credit_svc.grant("T1", 10, note="seed")
    result = charge_delivery_credits(
        firm_id="T1",
        channel_id="C1",
        file_id=f"F-{pages}-{appended}",
        kind="invoice",
        payload={"input_page_count": pages},
        append_result={"appended": appended, "deduped": 0},
        input_page_count=pages,
    )
    assert result is not None
    expected_used = max(pages, appended, 1)
    assert result["credits_used"] == expected_used
    assert result["credits_remaining"] == expected_balance_after
    assert _credit_svc.read_balance("T1") == expected_balance_after


def test_estimate_units_from_bytes_minimum_one() -> None:
    assert estimate_units_from_bytes(b"%PDF-1.4", "application/pdf") >= 1
