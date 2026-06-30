from accounting_agents.batch_schemas import BatchResult, ReviewRequest, SoftWarning
from ledgr_agent.internal.schemas import CreditSummary
 
 
def test_batch_result_minimal_success_payload() -> None:
    result = BatchResult(
        status="success",
        client_id="client_demo",
        firm_id="team_demo",
        source_files=["invoice.pdf"],
        credits=CreditSummary(
            credits_estimated=1,
            credits_used=1,
            credits_remaining=9,
            credit_status="charged",
        ),
    )
 
    dumped = result.model_dump()
 
    assert dumped["status"] == "success"
    assert dumped["credits"]["credits_used"] == 1
    assert dumped["review_requests"] == []
    assert dumped["soft_warnings"] == []
 
 
def test_batch_result_keeps_hard_review_and_soft_warning_separate() -> None:
    result = BatchResult(
        status="needs_review",
        client_id="client_demo",
        firm_id="team_demo",
        source_files=["invoice.pdf"],
        review_requests=[
            ReviewRequest(
                id="missing_invoice_number",
                severity="hard_review",
                message="Invoice number is missing.",
            )
        ],
        soft_warnings=[
            SoftWarning(
                id="low_coa_confidence_group",
                message="11 lines have low-confidence account mapping.",
                count=11,
            )
        ],
    )
 
    assert result.review_requests[0].severity == "hard_review"
    assert result.soft_warnings[0].count == 11
