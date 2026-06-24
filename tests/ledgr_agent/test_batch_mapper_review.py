from invoice_processing.pipeline import BatchResult as EngineBatchResult, ProcessedDoc, DocRoute
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
