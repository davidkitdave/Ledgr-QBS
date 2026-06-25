from ledgr_agent.slack.hitl_bridge import (
    approval_summary_from_batch,
    apply_edits_to_ledger_payload,
    ledger_rows_to_edit_lines,
    op_id_for_file,
    should_pause_for_hitl,
)


def test_op_id_for_file_matches_graph_convention() -> None:
    assert op_id_for_file("C1", "F9") == "C1:F9"


def test_should_pause_on_needs_review_status() -> None:
    assert should_pause_for_hitl({"status": "needs_review"}) is True


def test_should_not_pause_on_success_without_hard_review() -> None:
    batch = {
        "status": "success",
        "soft_warnings": [{"id": "low_coa", "message": "3 lines low confidence", "count": 3}],
    }
    assert should_pause_for_hitl(batch) is False


def test_should_pause_on_hard_review_even_if_status_success() -> None:
    batch = {
        "status": "success",
        "review_requests": [{"id": "x", "severity": "hard_review", "message": "GST issue"}],
    }
    assert should_pause_for_hitl(batch) is True


def test_approval_summary_lists_review_and_soft_warnings() -> None:
    summary = approval_summary_from_batch(
        {
            "review_requests": [{"message": "missing vendor", "severity": "hard_review"}],
            "soft_warnings": [{"message": "2 lines low COA confidence", "count": 2}],
        }
    )
    assert "missing vendor" in summary
    assert "2 lines low COA confidence" in summary


def test_ledger_rows_to_edit_lines_maps_qbs_columns() -> None:
    lines = ledger_rows_to_edit_lines(
        {
            "batches": [
                {
                    "rows": [
                        {
                            "Description": "Widget",
                            "Account": "6100",
                            "Amount": 50.0,
                        }
                    ]
                }
            ]
        }
    )
    assert lines == [
        {
            "description": "Widget",
            "account_code": "6100",
            "tax_treatment": "",
            "net_amount": 50.0,
        }
    ]


def test_apply_edits_to_ledger_payload_updates_account_and_amount() -> None:
    payload = {
        "batches": [
            {
                "sheet": "Purchase",
                "rows": [{"Description": "Widget", "Account": "6100", "Amount": 50.0}],
            }
        ]
    }
    updated = apply_edits_to_ledger_payload(
        payload,
        {"lines": [{"index": 0, "account_code": "6200", "net_amount": 55.0}]},
    )
    row = updated["batches"][0]["rows"][0]
    assert row["Account"] == "6200"
    assert row["Amount"] == 55.0
