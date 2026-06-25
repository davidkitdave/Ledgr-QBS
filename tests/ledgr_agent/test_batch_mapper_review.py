from io import BytesIO

from openpyxl import Workbook

from invoice_processing.pipeline import BatchResult as EngineBatchResult, ProcessedDoc, DocRoute
from ledgr_agent.schemas.credit import CreditSummary
from ledgr_agent.tools.batch_mapper import map_engine_batch_to_contract


class _Client:
    client_id = "playground"
    firm_id = "team_demo"


def _doc(note: str, reconciled: bool = False) -> ProcessedDoc:
    route = DocRoute(
        fy=2025,
        bucket="purchase",
        archive_path="eval-default/FY2025/purchase/stub.pdf",
        workbook="Ledger_FY2025.xlsx",
        sheet="Purchase",
    )
    return ProcessedDoc(
        path="/tmp/inv.pdf",
        doc_type="invoice",
        direction="purchase",
        normalized=None,
        bank=None,
        reconciled=reconciled,
        note=note,
        route=route,
    )


def test_map_engine_batch_groups_account_flags() -> None:
    reasons = [f"line {i} flagged for account review" for i in range(5)]
    docs = [_doc("needs review: " + "; ".join(reasons))]
    engine = EngineBatchResult(docs=docs, errors=[], workbooks={})

    batch = map_engine_batch_to_contract(
        engine,
        client=_Client(),
        source_files=["/tmp/inv.pdf"],
        missing_files=[],
    )

    assert batch.status == "needs_review"
    assert len(batch.review_requests) == 0
    assert len(batch.soft_warnings) == 1
    assert batch.soft_warnings[0].count == 5


def test_map_engine_batch_does_not_review_reconciled_ok_note() -> None:
    engine = EngineBatchResult(
        docs=[_doc("Lines total $700.00 · Footer $700.00 · OK", reconciled=True)],
        errors=[],
        workbooks={},
    )

    batch = map_engine_batch_to_contract(
        engine,
        client=_Client(),
        source_files=["/tmp/inv.pdf"],
        missing_files=[],
    )

    assert batch.status == "success"
    assert batch.review_requests == []
    assert batch.soft_warnings == []


def test_map_engine_batch_exposes_erp_headers_and_rows() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Purchase"
    sheet.append(["Date", "Description", "Account", "Amount"])
    sheet.append(["2026-06-24", "Office supplies", "6100", 109.0])
    buffer = BytesIO()
    workbook.save(buffer)

    engine = EngineBatchResult(
        docs=[_doc("OK", reconciled=True)],
        errors=[],
        workbooks={"Ledger_FY2026.xlsx": buffer.getvalue()},
    )

    batch = map_engine_batch_to_contract(
        engine,
        client=_Client(),
        source_files=["/tmp/inv.pdf"],
        missing_files=[],
    )

    assert batch.erp_exports[0]["sheets"][0]["headers"] == [
        "Date",
        "Description",
        "Account",
        "Amount",
    ]
    assert batch.export_rows == [
        {
            "workbook": "Ledger_FY2026.xlsx",
            "sheet": "Purchase",
            "Date": "2026-06-24",
            "Description": "Office supplies",
            "Account": "6100",
            "Amount": 109.0,
        }
    ]


def test_map_engine_batch_accepts_visible_credit_summary() -> None:
    engine = EngineBatchResult(docs=[_doc("OK", reconciled=True)], errors=[], workbooks={})

    batch = map_engine_batch_to_contract(
        engine,
        client=_Client(),
        source_files=["/tmp/inv.pdf"],
        missing_files=[],
        credits=CreditSummary(
            credits_estimated=1,
            credits_used=0,
            credits_remaining=9,
            credit_status="estimated",
        ),
    )

    assert batch.credits.credit_status == "estimated"
    assert batch.credits.credits_estimated == 1
    assert batch.credits.credits_remaining == 9
